from __future__ import annotations

import json
import os
import re
import base64
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from PIL import Image, ImageDraw, ImageEnhance

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.api.dependencies import RequestContext, get_container, get_request_context, require_operator_context
from app.api.routes.product_api_contracts import (
    BriefResponse,
    CommitmentCandidateOut,
    CommitmentCandidateReviewIn,
    CommitmentCandidateStageIn,
    CommitmentCreateIn,
    CommitmentExtractIn,
    CommitmentOut,
    DeadlineOut,
    DeadlineResponse,
    DecisionItemOut,
    DecisionQueueItemOut,
    DecisionResponse,
    DraftApproveIn,
    DraftCandidateOut,
    EvidenceItemOut,
    EvidenceResponse,
    HandoffAssignIn,
    HandoffAssignmentHistoryOut,
    HandoffCompleteIn,
    HandoffNoteOut,
    HistoryEntryOut,
    OperatorCenterActionOut,
    OperatorCenterLaneOut,
    OperatorCenterOut,
    PersonCorrectionIn,
    PreferenceCorrectionApplyIn,
    PreferenceCorrectionApplyOut,
    PreferenceDecisionAssessmentIn,
    PreferenceDecisionAssessmentOut,
    PreferenceEvidenceApplyOut,
    PreferenceEvidenceEventIn,
    PreferenceMailboxImportIn,
    PreferenceMailboxImportOut,
    PreferenceLearningSummaryOut,
    PreferenceNodeArchiveIn,
    PreferenceNodeOut,
    PreferenceNodeUpsertIn,
    PreferenceProfileBundleOut,
    PreferenceProfileSummaryOut,
    PropertyDecisionRecordIn,
    PropertyFeedbackRecordIn,
    PropertyFeedbackRecordOut,
    PropertyDecisionCopilotIn,
    PropertyDecisionCopilotOut,
    PropertyDecisionStateOut,
    PropertyAgentQuestionTaskUpdateIn,
    PropertyDocumentRecordUpdateIn,
    PropertyMagicFitSceneCreateIn,
    PropertyMagicFitReferenceAssetOut,
    PropertyMagicFitReferenceUploadIn,
    PropertyMagicFitReferenceUploadOut,
    PropertyMagicFitSceneOut,
    PropertyFeedbackSuggestionRequestIn,
    PropertyFeedbackSuggestionSetOut,
    PreferenceProfileUpsertIn,
    PersonDetailOut,
    PersonProfileOut,
    QueueResolveIn,
    QueueResponse,
    SearchResponse,
    SearchResultOut,
    WorkspaceInvitationAcceptIn,
    WorkspaceAccessSessionCreateIn,
    WorkspaceAccessSessionOut,
    WorkspaceAccessSessionResponse,
    WorkspaceInvitationCreateIn,
    WorkspaceInvitationOut,
    WorkspaceInvitationResponse,
    RuleItemOut,
    RuleResponse,
    RuleSimulateIn,
    ThreadItemOut,
    ThreadResponse,
    WorkspaceDiagnosticsOut,
    WorkspacePlanDetailOut,
    WorkspaceOutcomesOut,
    WorkspaceSupportBundleOut,
    WorkspaceTrustOut,
    WorkspaceUsageDetailOut,
    brief_out,
    commitment_candidate_out,
    commitment_out,
    deadline_out,
    decision_out,
    draft_out,
    evidence_item_out,
    handoff_assignment_history_out,
    handoff_out,
    history_out,
    now_iso,
    person_detail_out,
    person_out,
    queue_out,
    rule_out,
    thread_out,
)
from app.container import AppContainer
from app.product.service import _property_feedback_reason_map, build_product_service
from app.product.property_opportunities import (
    find_property_search_candidate,
    property_opportunity_public_projection,
)
from app.services.fliplink import build_fliplink_packet_service
from app.services import poppy_ai as poppy_ai_service
from app.services import brilliant_directories as brilliant_directories_service
from app.services.dadan_feedback import dadan_feedback_signals
from app.services.dadan import DadanVideoRequestService
from app.services.heyy_whatsapp_service import HeyyWhatsAppBridgeService
from app.services.heyy_whatsapp_service import heyy_daily_template_budget, redact_phone_number, require_heyy_enabled
from app.services.dossier_writer import write_verified_dossier_from_research
from app.services.dossier_writer.neuronwriter_adapter import create_neuronwriter_query
from app.services.registration_email import property_notification_preview

router = APIRouter(prefix="/app/api", tags=["product"])
public_router = APIRouter(prefix="/app/api", tags=["product-public-assets"])

_PROPERTY_DECISION_REASON_KEYS: frozenset[str] = frozenset(
    {
        "no_floorplan",
        "floorplan_missing",
        "operating_costs_missing",
        "energy_certificate_missing",
        "heating_unclear",
        "noise_risk",
    }
)
_PROPERTY_MAP_PREVIEW_PENDING_LABEL = "Preparing map"


def _api_safe_token(value: object, fallback: str = "ref") -> str:
    token = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return token[:120] or fallback


def _magic_fit_reference_root(container: AppContainer, *, principal_id: str) -> Path:
    base = Path(str(container.settings.storage.artifacts_dir)).resolve() / "magic_fit_refs"
    return base / _api_safe_token(principal_id, "principal")


def _property_map_preview_root(container: AppContainer) -> Path:
    configured_root = os.environ.get("EA_ARTIFACTS_DIR") or container.settings.storage.artifacts_dir or "/data/artifacts"
    root = Path(str(configured_root)).resolve()
    return root / "map_previews"


def _property_map_preview_missing_png() -> bytes:
    image = Image.new("RGB", (640, 368), color=(240, 235, 226))
    draw = ImageDraw.Draw(image, "RGBA")
    road = (204, 197, 187, 168)
    water = (198, 213, 217, 138)
    park = (207, 219, 199, 128)
    red = (194, 42, 48, 72)
    red_stroke = (132, 30, 36, 154)
    draw.rectangle((0, 0, 640, 368), fill=(240, 235, 226, 255))
    draw.polygon([(0, 34), (190, 0), (438, 0), (640, 60), (640, 124), (418, 86), (222, 114), (0, 88)], fill=park)
    draw.polygon([(0, 268), (148, 244), (318, 278), (640, 250), (640, 368), (0, 368)], fill=water)
    for offset in (-120, 40, 210, 392):
        draw.line([(offset, 0), (offset + 260, 368)], fill=road, width=18)
        draw.line([(offset, 0), (offset + 260, 368)], fill=(255, 253, 247, 110), width=5)
    for y in (72, 162, 238, 310):
        draw.line([(0, y), (640, y - 34)], fill=road, width=14)
        draw.line([(0, y), (640, y - 34)], fill=(255, 253, 247, 105), width=4)
    overlay = [(188, 96), (438, 78), (510, 178), (456, 278), (228, 294), (126, 206)]
    draw.polygon(overlay, fill=red)
    draw.line(overlay + [overlay[0]], fill=(255, 250, 242, 150), width=4, joint="curve")
    draw.line(overlay + [overlay[0]], fill=red_stroke, width=2, joint="curve")
    image = ImageEnhance.Color(image).enhance(0.50)
    image = ImageEnhance.Contrast(image).enhance(0.84)
    image = ImageEnhance.Brightness(image).enhance(1.04)
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class StructuredPropertyFeedbackIn(BaseModel):
    stakeholder_id: str = Field(min_length=1, max_length=160)
    stakeholder_label: str = Field(default="", max_length=160)
    property_ref: str = Field(min_length=1, max_length=500)
    publication_id: str = Field(default="", max_length=160)
    share_id: str = Field(default="", max_length=160)
    audience_type: str = Field(default="", max_length=80)
    category: str = Field(min_length=1, max_length=80)
    sentiment: str = Field(default="", max_length=80)
    importance: int = Field(default=3, ge=1, le=5)
    text: str = Field(default="", max_length=2000)
    source: str = Field(default="packet", max_length=80)
    source_event_id: str = Field(default="", max_length=160)
    decision_state: str = Field(default="", max_length=80)
    followup_status: str = Field(default="", max_length=80)


class PropertyFeedbackStatusUpdateIn(BaseModel):
    followup_status: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class DadanPropertyFeedbackIn(BaseModel):
    property_ref: str = Field(min_length=1, max_length=500)
    publication_id: str = Field(default="", max_length=160)
    share_id: str = Field(default="", max_length=160)
    audience_type: str = Field(default="viewer", max_length=80)
    stakeholder_id: str = Field(default="", max_length=160)
    stakeholder_label: str = Field(default="", max_length=160)
    event_id: str = Field(default="", max_length=160)
    submission_id: str = Field(default="", max_length=160)
    video_id: str = Field(default="", max_length=160)
    video_url: str = Field(default="", max_length=1000)
    answers: list[dict[str, object]] = Field(default_factory=list)
    responses: list[dict[str, object]] = Field(default_factory=list)
    transcript: str = Field(default="", max_length=4000)
    summary: str = Field(default="", max_length=4000)
    comment: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=2000)
    owner_review_confirmed: bool = False


class DadanVideoRequestCreateIn(BaseModel):
    property_ref: str = Field(min_length=1, max_length=500)
    property_url: str = Field(default="", max_length=1000)
    request_kind: str = Field(default="agent_missing_fact", max_length=80)
    audience_type: str = Field(default="agent", max_length=80)
    title: str = Field(default="", max_length=240)
    instructions: str = Field(default="", max_length=4000)
    metadata: dict[str, object] = Field(default_factory=dict)


class PropertySummaryGenerateIn(BaseModel):
    subject_type: str = Field(default="property", max_length=80)
    subject_id: str = Field(min_length=1, max_length=500)
    artifact_type: str = Field(min_length=1, max_length=80)
    audience_type: str = Field(default="family", max_length=80)


class PropertyOpportunityGenerateIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=200)
    artifact_type: str = Field(default="why_shortlisted", max_length=80)
    audience_type: str = Field(default="family", max_length=80)


class PropertyOpportunityConceptCoverIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=200)


class FollowupAssignIn(BaseModel):
    owner: str = Field(min_length=1, max_length=160)


class FollowupResolveIn(BaseModel):
    resolution: str = Field(min_length=1, max_length=240)


class PropertyNeuronWriterQueryIn(BaseModel):
    keyword: str = Field(min_length=1, max_length=240)
    project_id: str = Field(min_length=1, max_length=160)
    language: str = Field(default="German", max_length=80)
    engine: str = Field(default="google.at", max_length=80)
    content_mode: str = Field(default="public_market_report", max_length=80)


class PropertyDossierWriteIn(BaseModel):
    packet_kind: str = Field(default="owner_review", max_length=80)
    privacy_mode: str = Field(default="owner_private", max_length=80)
    language: str = Field(default="German", max_length=80)
    writer_mode: str = Field(default="claim_bound", max_length=80)
    neuronwriter_query_id: str = Field(default="", max_length=240)
    research: dict[str, object] = Field(default_factory=dict)


class HeyyWhatsAppTemplateSendIn(BaseModel):
    phone_number: str = Field(min_length=1, max_length=80)
    template_id: str = Field(min_length=1, max_length=160)
    channel_id: str = Field(default="", max_length=160)
    variables: list[dict[str, object]] = Field(default_factory=list)


class HeyyPropertyMatchNotificationIn(BaseModel):
    phone_number: str = Field(min_length=1, max_length=80)
    template_id: str = Field(min_length=1, max_length=160)
    channel_id: str = Field(default="", max_length=160)
    property_ref: str = Field(min_length=1, max_length=500)
    property_title: str = Field(min_length=1, max_length=240)
    fit_score: str = Field(default="", max_length=80)
    reason: str = Field(default="", max_length=240)
    missing_fact: str = Field(default="", max_length=240)


class HeyySearchDigestNotificationIn(BaseModel):
    phone_number: str = Field(min_length=1, max_length=80)
    template_id: str = Field(min_length=1, max_length=160)
    channel_id: str = Field(default="", max_length=160)
    search_agent_id: str = Field(min_length=1, max_length=160)
    agent_name: str = Field(min_length=1, max_length=160)
    homes_checked: str = Field(default="", max_length=80)
    ranked_count: str = Field(default="", max_length=80)
    top_fit_score: str = Field(default="", max_length=80)
    held_back_count: str = Field(default="", max_length=80)


def _require_operator_for_workspace_role(*, role: str, operator_id: str = "", context: RequestContext) -> None:
    normalized_role = str(role or "principal").strip().lower() or "principal"
    if normalized_role == "operator" or str(operator_id or "").strip():
        require_operator_context(context)


def _heyy_selected_for_principal(*, container: AppContainer, principal_id: str) -> bool:
    try:
        status = container.onboarding.status(principal_id=principal_id)
    except Exception:
        return False
    selected = {
        str(item or "").strip().lower()
        for item in list(status.get("selected_channels") or [])
        if str(item or "").strip()
    }
    return "whatsapp" in selected


def _heyy_template_budget_ok(*, container: AppContainer, principal_id: str) -> bool:
    limit = heyy_daily_template_budget()
    if limit <= 0:
        return False
    packet_service = build_fliplink_packet_service(container)
    rows = packet_service.list_events(principal_id=principal_id, event_type="heyy_whatsapp_template_sent", limit=500)
    now = datetime.now(timezone.utc)
    sent_today = 0
    for row in rows:
        created_at = str(row.get("created_at") or row.get("recorded_at") or "").strip()
        if not created_at:
            continue
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.astimezone(timezone.utc).date() == now.date():
            sent_today += 1
        if sent_today >= limit:
            return False
    return True


def _heyy_latest_opt_out_command(*, container: AppContainer, principal_id: str, phone_number: str = "") -> str:
    packet_service = build_fliplink_packet_service(container)
    target_hash = redact_phone_number(phone_number).get("phone_e164_hash", "")
    rows = packet_service.list_events(principal_id=principal_id, event_type="heyy_whatsapp_message_received", limit=100)
    for row in rows:
        payload = dict(row.get("payload_json") or {}) if isinstance(row.get("payload_json"), dict) else {}
        command = str(payload.get("opt_command") or "").strip().upper()
        if command not in {"STOP", "START", "PAUSE"}:
            continue
        event_hash = str(payload.get("phone_e164_hash") or "").strip()
        if target_hash and event_hash and event_hash != target_hash:
            continue
        if command == "START":
            return ""
        if command in {"STOP", "PAUSE"}:
            return command
    return ""


def _require_heyy_send_allowed(*, container: AppContainer, principal_id: str, phone_number: str = "") -> None:
    if not _heyy_selected_for_principal(container=container, principal_id=principal_id):
        raise HTTPException(status_code=409, detail="heyy_whatsapp_not_opted_in")
    if _heyy_latest_opt_out_command(container=container, principal_id=principal_id, phone_number=phone_number):
        raise HTTPException(status_code=409, detail="heyy_whatsapp_stopped")
    if not _heyy_template_budget_ok(container=container, principal_id=principal_id):
        raise HTTPException(status_code=429, detail="heyy_daily_template_budget_exhausted")


@router.post("/property/content-intelligence/briefs/{brief_id}/neuronwriter-query")
def create_property_neuronwriter_query(
    brief_id: str,
    payload: PropertyNeuronWriterQueryIn,
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    require_operator_context(context)
    allowed_modes = {"public_content", "public_market_report", "public_city_guide"}
    if payload.content_mode not in allowed_modes:
        return {
            "status": "blocked",
            "brief_id": brief_id,
            "reason": "neuronwriter_private_or_non_public_content_blocked",
            "content_mode": payload.content_mode,
        }
    recommendation = create_neuronwriter_query(
        keyword=payload.keyword,
        project_id=payload.project_id,
        language=payload.language,
        engine=payload.engine,
    )
    result = recommendation.model_dump()
    result.update({"brief_id": brief_id, "content_mode": payload.content_mode})
    return result


@router.post("/property/dossiers/{dossier_id}/write")
def write_property_dossier(
    dossier_id: str,
    payload: PropertyDossierWriteIn,
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    require_operator_context(context)
    if payload.writer_mode != "claim_bound":
        raise HTTPException(status_code=400, detail="unsupported_dossier_writer_mode")
    verified = write_verified_dossier_from_research(
        dossier_id=dossier_id,
        research=payload.research,
        packet_kind=payload.packet_kind,
        privacy_mode=payload.privacy_mode,
        language=payload.language,
        neuronwriter_query_id=payload.neuronwriter_query_id,
    )
    return {
        "status": "written" if verified.status == "verified" else "rejected",
        "dossier_id": dossier_id,
        "sections": [section.model_dump() for section in verified.draft.sections],
        "claim_coverage": verified.claim_coverage,
        "unsupported_sentences": verified.unsupported_sentences,
        "forbidden_hits": verified.forbidden_hits,
        "neuronwriter_applied": bool(verified.neuronwriter and verified.neuronwriter.status == "ready"),
        "neuronwriter": verified.neuronwriter.model_dump() if verified.neuronwriter else None,
        "privacy_check": "passed" if verified.status == "verified" else "failed",
        "generated_by": verified.draft.generated_by,
    }

@router.get("/brief", response_model=BriefResponse)
def get_brief(
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> BriefResponse:
    service = build_product_service(container)
    items = service.list_brief_items(
        principal_id=context.principal_id,
        limit=limit,
        operator_id=str(context.operator_id or "").strip(),
    )
    return BriefResponse(generated_at=now_iso(), items=[brief_out(item) for item in items], total=len(items))


@router.get("/queue", response_model=QueueResponse)
def get_queue(
    limit: int = Query(default=30, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> QueueResponse:
    service = build_product_service(container)
    items = service.list_queue(
        principal_id=context.principal_id,
        limit=limit,
        operator_id=str(context.operator_id or "").strip(),
    )
    return QueueResponse(generated_at=now_iso(), items=[queue_out(item) for item in items], total=len(items))


@router.get("/search", response_model=SearchResponse)
def search_workspace(
    q: str = Query(default="", alias="query"),
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> SearchResponse:
    service = build_product_service(container)
    items = service.search_workspace(
        principal_id=context.principal_id,
        query=q,
        limit=limit,
        operator_id=str(context.operator_id or "").strip(),
    )
    if str(q or "").strip():
        service.record_surface_event(
            principal_id=context.principal_id,
            event_type="workspace_search_performed",
            surface="search_api",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
            metadata={"query": str(q or "").strip()[:80], "result_total": len(items)},
        )
    return SearchResponse(generated_at=now_iso(), items=[SearchResultOut(**item) for item in items], total=len(items))


@router.get("/providers/poppy/verify")
def verify_poppy_provider(
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    require_operator_context(context)
    return poppy_ai_service.poppy_verify_account()


@router.get("/providers/poppy/boards")
def list_poppy_boards(
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    require_operator_context(context)
    return poppy_ai_service.poppy_list_boards()


@router.get("/providers/poppy/boards/{board_id}/chats")
def list_poppy_board_chats(
    board_id: str,
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    require_operator_context(context)
    return poppy_ai_service.poppy_list_chats(board_id=board_id)


@router.get("/providers/poppy/ask")
def ask_poppy_knowledge_base(
    board_id: str = Query(min_length=1),
    chat_id: str = Query(min_length=1),
    prompt: str = Query(min_length=1),
    model: str = Query(default=""),
    additional_context: str = Query(default=""),
    plaintext: bool = Query(default=True),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    require_operator_context(context)
    return poppy_ai_service.poppy_ask_knowledge_base(
        board_id=board_id,
        chat_id=chat_id,
        prompt=prompt,
        model=model,
        additional_context=additional_context,
        plaintext=plaintext,
    )


@router.get("/decisions", response_model=DecisionResponse)
def list_decisions(
    limit: int = Query(default=20, ge=1, le=100),
    include_closed: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DecisionResponse:
    service = build_product_service(container)
    items = service.list_decisions(principal_id=context.principal_id, limit=limit, include_closed=include_closed)
    return DecisionResponse(generated_at=now_iso(), items=[decision_out(item) for item in items], total=len(items))


@router.get("/decisions/{decision_ref:path}/history", response_model=list[HistoryEntryOut])
def get_decision_history(
    decision_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[HistoryEntryOut]:
    service = build_product_service(container)
    found = service.get_decision(principal_id=context.principal_id, decision_ref=decision_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="decision_not_found")
    return [history_out(item) for item in service.get_decision_history(principal_id=context.principal_id, decision_ref=decision_ref, limit=limit)]


@router.get("/decisions/{decision_ref:path}", response_model=DecisionItemOut)
def get_decision(
    decision_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DecisionItemOut:
    service = build_product_service(container)
    found = service.get_decision(principal_id=context.principal_id, decision_ref=decision_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="decision_not_found")
    return decision_out(found)


@router.post("/decisions/{decision_ref:path}/resolve", response_model=DecisionItemOut)
def resolve_decision(
    decision_ref: str,
    body: QueueResolveIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DecisionItemOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "product").strip()
    updated = service.resolve_decision(
        principal_id=context.principal_id,
        decision_ref=decision_ref,
        actor=actor,
        action=body.action,
        reason=body.reason,
        due_at=body.due_at,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="decision_not_found")
    return decision_out(updated)


@router.get("/deadlines", response_model=DeadlineResponse)
def list_deadlines(
    limit: int = Query(default=20, ge=1, le=100),
    include_closed: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DeadlineResponse:
    service = build_product_service(container)
    items = service.list_deadlines(principal_id=context.principal_id, limit=limit, include_closed=include_closed)
    return DeadlineResponse(generated_at=now_iso(), items=[deadline_out(item) for item in items], total=len(items))


@router.get("/deadlines/{deadline_ref:path}/history", response_model=list[HistoryEntryOut])
def get_deadline_history(
    deadline_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[HistoryEntryOut]:
    service = build_product_service(container)
    found = service.get_deadline(principal_id=context.principal_id, deadline_ref=deadline_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="deadline_not_found")
    return [history_out(item) for item in service.get_deadline_history(principal_id=context.principal_id, deadline_ref=deadline_ref, limit=limit)]


@router.get("/deadlines/{deadline_ref:path}", response_model=DeadlineOut)
def get_deadline(
    deadline_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DeadlineOut:
    service = build_product_service(container)
    found = service.get_deadline(principal_id=context.principal_id, deadline_ref=deadline_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="deadline_not_found")
    return deadline_out(found)


@router.post("/deadlines/{deadline_ref:path}/resolve", response_model=DeadlineOut)
def resolve_deadline(
    deadline_ref: str,
    body: QueueResolveIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DeadlineOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "product").strip()
    updated = service.resolve_deadline(
        principal_id=context.principal_id,
        deadline_ref=deadline_ref,
        actor=actor,
        action=body.action,
        reason=body.reason,
        due_at=body.due_at,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="deadline_not_found")
    return deadline_out(updated)


@router.post("/queue/{item_ref:path}/resolve", response_model=DecisionQueueItemOut)
def resolve_queue_item(
    item_ref: str,
    body: QueueResolveIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DecisionQueueItemOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "product").strip()
    updated = service.resolve_queue_item(
        principal_id=context.principal_id,
        item_ref=item_ref,
        action=body.action,
        actor=actor,
        reason=body.reason,
        reason_code=body.reason_code,
        due_at=body.due_at,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="queue_item_not_found")
    return queue_out(updated)


@router.get("/threads", response_model=ThreadResponse)
def list_threads(
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> ThreadResponse:
    service = build_product_service(container)
    items = service.list_threads(principal_id=context.principal_id, limit=limit)
    return ThreadResponse(generated_at=now_iso(), items=[thread_out(item) for item in items], total=len(items))


@router.get("/threads/{thread_ref:path}/history", response_model=list[HistoryEntryOut])
def get_thread_history(
    thread_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[HistoryEntryOut]:
    service = build_product_service(container)
    if service.get_thread(principal_id=context.principal_id, thread_ref=thread_ref) is None:
        raise HTTPException(status_code=404, detail="thread_not_found")
    return [history_out(item) for item in service.get_thread_history(principal_id=context.principal_id, thread_ref=thread_ref, limit=limit)]


@router.get("/threads/{thread_ref:path}", response_model=ThreadItemOut)
def get_thread(
    thread_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> ThreadItemOut:
    service = build_product_service(container)
    found = service.get_thread(principal_id=context.principal_id, thread_ref=thread_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="thread_not_found")
    return thread_out(found)


@router.post("/threads/{thread_ref:path}/resume-delivery", response_model=HandoffNoteOut)
def resume_thread_delivery(
    thread_ref: str,
    body: HandoffAssignIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HandoffNoteOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or body.operator_id or "product").strip()
    try:
        reopened = service.resume_thread_delivery_followup(
            principal_id=context.principal_id,
            thread_ref=thread_ref,
            actor=actor,
            operator_id=body.operator_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if reopened is None:
        raise HTTPException(status_code=404, detail="thread_not_found")
    return handoff_out(reopened)


@router.get("/commitments", response_model=list[CommitmentOut])
def list_commitments(
    limit: int = Query(default=50, ge=1, le=200),
    include_closed: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[CommitmentOut]:
    service = build_product_service(container)
    return [
        commitment_out(item)
        for item in service.list_commitments(principal_id=context.principal_id, limit=limit, include_closed=include_closed)
    ]


@router.post("/commitments", response_model=CommitmentOut)
def create_commitment(
    body: CommitmentCreateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> CommitmentOut:
    service = build_product_service(container)
    created = service.create_commitment(
        principal_id=context.principal_id,
        title=body.title,
        details=body.details,
        due_at=body.due_at,
        priority=body.priority,
        counterparty=body.counterparty,
        owner=body.owner,
        kind=body.kind,
        stakeholder_id=body.stakeholder_id,
        channel_hint=body.channel_hint,
    )
    return commitment_out(created)


@router.post("/commitments/extract", response_model=list[CommitmentCandidateOut])
def extract_commitments(
    body: CommitmentExtractIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[CommitmentCandidateOut]:
    service = build_product_service(container)
    rows = service.extract_commitments(
        text=body.text,
        counterparty=body.counterparty,
        due_at=body.due_at,
    )
    return [commitment_candidate_out(row) for row in rows]


@router.get("/commitments/candidates", response_model=list[CommitmentCandidateOut])
def list_commitment_candidates(
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[CommitmentCandidateOut]:
    service = build_product_service(container)
    return [
        commitment_candidate_out(row)
        for row in service.list_commitment_candidates(principal_id=context.principal_id, limit=limit, status=status)
    ]


@router.post("/commitments/candidates/stage", response_model=list[CommitmentCandidateOut])
def stage_commitment_candidates(
    body: CommitmentCandidateStageIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[CommitmentCandidateOut]:
    service = build_product_service(container)
    rows = service.stage_extracted_commitments(
        principal_id=context.principal_id,
        text=body.text,
        counterparty=body.counterparty,
        due_at=body.due_at,
        kind=body.kind,
        stakeholder_id=body.stakeholder_id,
    )
    return [commitment_candidate_out(row) for row in rows]


@router.post("/commitments/candidates/{candidate_id}/accept", response_model=CommitmentOut)
def accept_commitment_candidate(
    candidate_id: str,
    body: CommitmentCandidateReviewIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> CommitmentOut:
    service = build_product_service(container)
    created = service.accept_commitment_candidate(
        principal_id=context.principal_id,
        candidate_id=candidate_id,
        reviewer=body.reviewer,
        title=body.title,
        details=body.details,
        due_at=body.due_at,
        counterparty=body.counterparty,
        kind=body.kind,
        stakeholder_id=body.stakeholder_id,
    )
    if created is None:
        raise HTTPException(status_code=404, detail="commitment_candidate_not_found")
    return commitment_out(created)


@router.post("/commitments/candidates/{candidate_id}/reject", response_model=CommitmentCandidateOut)
def reject_commitment_candidate(
    candidate_id: str,
    body: CommitmentCandidateReviewIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> CommitmentCandidateOut:
    service = build_product_service(container)
    rejected = service.reject_commitment_candidate(principal_id=context.principal_id, candidate_id=candidate_id, reviewer=body.reviewer)
    if rejected is None:
        raise HTTPException(status_code=404, detail="commitment_candidate_not_found")
    return commitment_candidate_out(rejected)


@router.get("/commitments/{commitment_ref:path}/history", response_model=list[HistoryEntryOut])
def get_commitment_history(
    commitment_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[HistoryEntryOut]:
    service = build_product_service(container)
    found = service.get_commitment(principal_id=context.principal_id, commitment_ref=commitment_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="commitment_not_found")
    return [history_out(item) for item in service.get_commitment_history(principal_id=context.principal_id, commitment_ref=commitment_ref, limit=limit)]


@router.post("/commitments/{commitment_ref:path}/resolve", response_model=CommitmentOut)
def resolve_commitment(
    commitment_ref: str,
    body: QueueResolveIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> CommitmentOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "product").strip()
    updated = service.resolve_commitment(
        principal_id=context.principal_id,
        commitment_ref=commitment_ref,
        action=body.action,
        actor=actor,
        reason=body.reason,
        reason_code=body.reason_code,
        due_at=body.due_at,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="commitment_not_found")
    return commitment_out(updated)


@router.get("/commitments/{commitment_ref:path}", response_model=CommitmentOut)
def get_commitment(
    commitment_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> CommitmentOut:
    service = build_product_service(container)
    found = service.get_commitment(principal_id=context.principal_id, commitment_ref=commitment_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="commitment_not_found")
    return commitment_out(found)


@router.get("/drafts", response_model=list[DraftCandidateOut])
def list_drafts(
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[DraftCandidateOut]:
    service = build_product_service(container)
    return [draft_out(item) for item in service.list_drafts(principal_id=context.principal_id, limit=limit)]


@router.post("/drafts/{draft_ref:path}/approve", response_model=DraftCandidateOut)
def approve_draft(
    draft_ref: str,
    body: DraftApproveIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DraftCandidateOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "product").strip()
    approved = service.approve_draft(
        principal_id=context.principal_id,
        draft_ref=draft_ref,
        decided_by=actor,
        reason=body.reason,
    )
    if approved is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return draft_out(approved)


@router.post("/drafts/{draft_ref:path}/reject", response_model=DraftCandidateOut)
def reject_draft(
    draft_ref: str,
    body: DraftApproveIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> DraftCandidateOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "product").strip()
    rejected = service.reject_draft(
        principal_id=context.principal_id,
        draft_ref=draft_ref,
        decided_by=actor,
        reason=body.reason,
    )
    if rejected is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return draft_out(rejected)


@router.get("/people", response_model=list[PersonProfileOut])
def list_people(
    limit: int = Query(default=25, ge=1, le=200),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[PersonProfileOut]:
    service = build_product_service(container)
    return [person_out(item) for item in service.list_people(principal_id=context.principal_id, limit=limit)]


@router.get("/people/{person_id}", response_model=PersonProfileOut)
def get_person(
    person_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PersonProfileOut:
    service = build_product_service(container)
    found = service.get_person(principal_id=context.principal_id, person_id=person_id)
    if found is None:
        raise HTTPException(status_code=404, detail="person_not_found")
    return person_out(found)


@router.get("/people/{person_id}/detail", response_model=PersonDetailOut)
def get_person_detail(
    person_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PersonDetailOut:
    service = build_product_service(container)
    found = service.get_person_detail(
        principal_id=context.principal_id,
        person_id=person_id,
        operator_id=str(context.operator_id or "").strip(),
    )
    if found is None:
        raise HTTPException(status_code=404, detail="person_not_found")
    return person_detail_out(found)


@router.post("/people/{person_id}/correct", response_model=PersonDetailOut)
def correct_person(
    person_id: str,
    body: PersonCorrectionIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PersonDetailOut:
    service = build_product_service(container)
    found = service.correct_person_profile(
        principal_id=context.principal_id,
        person_id=person_id,
        preferred_tone=body.preferred_tone,
        add_theme=body.add_theme,
        remove_theme=body.remove_theme,
        add_risk=body.add_risk,
        remove_risk=body.remove_risk,
    )
    if found is None:
        raise HTTPException(status_code=404, detail="person_not_found")
    return person_detail_out(found)


@router.get("/people/{person_id}/history", response_model=list[HistoryEntryOut])
def get_person_history(
    person_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[HistoryEntryOut]:
    service = build_product_service(container)
    found = service.get_person(principal_id=context.principal_id, person_id=person_id)
    if found is None:
        raise HTTPException(status_code=404, detail="person_not_found")
    return [history_out(item) for item in service.get_person_history(principal_id=context.principal_id, person_id=person_id, limit=limit)]


@router.get("/people/{person_id}/preference-profile", response_model=PreferenceProfileBundleOut)
def get_preference_profile(
    person_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceProfileBundleOut:
    service = build_product_service(container)
    return PreferenceProfileBundleOut(**service.get_preference_profile(principal_id=context.principal_id, person_id=person_id))


@router.post("/people/{person_id}/preference-profile", response_model=PreferenceProfileSummaryOut)
def upsert_preference_profile(
    person_id: str,
    body: PreferenceProfileUpsertIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceProfileSummaryOut:
    service = build_product_service(container)
    return PreferenceProfileSummaryOut(
        **service.upsert_preference_profile(
            principal_id=context.principal_id,
            person_id=person_id,
            display_name=body.display_name,
            profile_scope=body.profile_scope,
            consent_mode=body.consent_mode,
            learning_enabled=body.learning_enabled,
            high_stakes_domains_enabled=body.high_stakes_domains_enabled,
        )
    )


@router.post("/people/{person_id}/preference-profile/mailbox-import", response_model=PreferenceMailboxImportOut)
def import_preference_profile_mailbox_history(
    person_id: str,
    body: PreferenceMailboxImportIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceMailboxImportOut:
    if not body.consent_confirmed:
        raise HTTPException(status_code=422, detail="mailbox_import_consent_required")
    if not body.consent_note.strip():
        raise HTTPException(status_code=422, detail="mailbox_import_consent_note_required")
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        payload = service.import_property_mailbox_preferences(
            principal_id=context.principal_id,
            person_id=person_id,
            actor=actor,
            account_email=body.account_email,
            consent_note=body.consent_note,
            email_limit=body.email_limit,
            lookback_days=body.lookback_days,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PreferenceMailboxImportOut(**payload)


@router.post("/people/{person_id}/preference-profile/nodes", response_model=PreferenceNodeOut)
def upsert_preference_node(
    person_id: str,
    body: PreferenceNodeUpsertIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceNodeOut:
    service = build_product_service(container)
    return PreferenceNodeOut(
        **service.upsert_preference_node(
            principal_id=context.principal_id,
            person_id=person_id,
            domain=body.domain,
            category=body.category,
            key=body.key,
            value_json=body.value_json,
            strength=body.strength,
            confidence=body.confidence,
            source_mode=body.source_mode,
            status=body.status,
            decay_policy=body.decay_policy,
        )
    )


@router.post("/people/{person_id}/preference-profile/nodes/{node_id}/archive", response_model=PreferenceCorrectionApplyOut)
def archive_preference_node(
    person_id: str,
    node_id: str,
    body: PreferenceNodeArchiveIn | None = None,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceCorrectionApplyOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        result = service.archive_preference_node(
            principal_id=context.principal_id,
            person_id=person_id,
            node_id=node_id,
            reason=(body.reason if body is not None else ""),
            corrected_by=actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="preference_node_not_found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return PreferenceCorrectionApplyOut(**result)


@router.post("/people/{person_id}/preference-profile/corrections", response_model=PreferenceCorrectionApplyOut)
def apply_preference_correction(
    person_id: str,
    body: PreferenceCorrectionApplyIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceCorrectionApplyOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    return PreferenceCorrectionApplyOut(
        **service.apply_preference_correction(
            principal_id=context.principal_id,
            person_id=person_id,
            domain=body.domain,
            category=body.category,
            key=body.key,
            value_json=body.value_json,
            strength=body.strength,
            reason=body.reason,
            corrected_by=actor,
        )
    )


@router.post("/people/{person_id}/preference-profile/evidence", response_model=PreferenceEvidenceApplyOut)
def record_preference_evidence(
    person_id: str,
    body: PreferenceEvidenceEventIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceEvidenceApplyOut:
    service = build_product_service(container)
    return PreferenceEvidenceApplyOut(
        **service.record_preference_evidence(
            principal_id=context.principal_id,
            person_id=person_id,
            domain=body.domain,
            event_type=body.event_type,
            object_type=body.object_type,
            object_id=body.object_id,
            source_ref=body.source_ref,
            raw_signal_json=dict(body.raw_signal_json or {}),
            interpreted_signal_json=dict(body.interpreted_signal_json or {}),
            signal_strength=body.signal_strength,
            reversible=body.reversible,
        )
    )


@router.post("/people/{person_id}/preference-profile/assessments", response_model=PreferenceDecisionAssessmentOut)
def assess_preference_candidate(
    person_id: str,
    body: PreferenceDecisionAssessmentIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceDecisionAssessmentOut:
    service = build_product_service(container)
    result = service.assess_preference_candidate(
        principal_id=context.principal_id,
        person_id=person_id,
        domain=body.domain,
        object_type=body.object_type,
        object_id=body.object_id,
        object_payload=dict(body.object_payload or {}),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="preference_profile_not_found")
    return PreferenceDecisionAssessmentOut(**result)


@router.get("/people/{person_id}/preference-profile/learning-summary", response_model=PreferenceLearningSummaryOut)
def get_preference_learning_summary(
    person_id: str,
    domain: str = Query(default="willhaben", min_length=1),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PreferenceLearningSummaryOut:
    service = build_product_service(container)
    return PreferenceLearningSummaryOut(
        **service.property_feedback_learning_summary(
            principal_id=context.principal_id,
            person_id=person_id,
            domain=domain,
        )
    )


@router.post("/people/{person_id}/preference-profile/property-feedback/suggestions", response_model=PropertyFeedbackSuggestionSetOut)
def get_property_feedback_suggestions(
    person_id: str,
    body: PropertyFeedbackSuggestionRequestIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyFeedbackSuggestionSetOut:
    service = build_product_service(container)
    return PropertyFeedbackSuggestionSetOut(
        **service.property_feedback_suggestions(
            property_facts=dict(body.property_facts or {}),
            assessment=dict(body.assessment or {}) if body.assessment else None,
        )
    )


@router.post("/people/{person_id}/preference-profile/property-feedback", response_model=PropertyFeedbackRecordOut)
def record_property_feedback(
    person_id: str,
    body: PropertyFeedbackRecordIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyFeedbackRecordOut:
    service = build_product_service(container)
    packet_service = build_fliplink_packet_service(container)
    actor = str(body.actor or context.operator_id or context.access_email or context.principal_id or "browser").strip()
    allowed_reason_keys = set(_property_feedback_reason_map().keys()) | set(_PROPERTY_DECISION_REASON_KEYS)
    normalized_reason_keys = [
        str(item or "").strip().lower()
        for item in list(body.reason_keys or [])
        if str(item or "").strip()
    ]
    invalid_reason_keys = [
        key
        for key in normalized_reason_keys
        if len(key) > 80 or key not in allowed_reason_keys
    ]
    if invalid_reason_keys:
        raise HTTPException(status_code=422, detail="invalid_property_feedback_reason_key")
    result = service.record_property_feedback(
        principal_id=context.principal_id,
        person_id=person_id,
        property_slug=body.property_slug,
        property_url=body.property_url,
        property_title=body.property_title,
        property_facts=dict(body.property_facts or {}),
        reaction=body.reaction,
        reason_keys=normalized_reason_keys,
        note=body.note,
        actor=actor,
    )
    structured_property_refs: list[str] = []
    for candidate_ref in (
        body.property_slug,
        body.property_url,
        body.property_title,
        "property",
    ):
        normalized_ref = str(candidate_ref or "").strip()
        if normalized_ref and normalized_ref not in structured_property_refs:
            structured_property_refs.append(normalized_ref)
    structured_feedback_errors: list[str] = []
    structured_feedback_recorded = 0
    if structured_property_refs:
        reaction_category = {
            "like": "love",
            "maybe": "question",
            "dislike": "dealbreaker",
            "hide": "concern",
        }.get(str(body.reaction or "").strip().lower(), "concern")
        reaction_sentiment = {
            "like": "positive",
            "maybe": "neutral",
            "dislike": "negative",
            "hide": "negative",
        }.get(str(body.reaction or "").strip().lower(), "neutral")
        decision_state = {
            "like": "interested",
            "maybe": "maybe",
            "dislike": "rejected",
            "hide": "archived",
        }.get(str(body.reaction or "").strip().lower(), "")
        reason_labels = [
            str(_property_feedback_reason_map().get(key, {}).get("label") or key).strip()
            for key in normalized_reason_keys
        ]
        summary_text = " | ".join(
            part
            for part in (
                str(body.note or "").strip(),
                ", ".join(label for label in reason_labels if label),
                f"Decision: {str(body.reaction or '').strip().lower()}",
            )
            if str(part or "").strip()
        )
        source_event_id = str(result.get("evidence", {}).get("event", {}).get("event_id") or "").strip()
        for structured_property_ref in structured_property_refs:
            try:
                packet_service.record_structured_feedback(
                    principal_id=context.principal_id,
                    property_ref=structured_property_ref,
                    stakeholder_id=f"profile:{str(person_id or 'self').strip() or 'self'}",
                    stakeholder_label=str(person_id or "self").strip() or "self",
                    publication_id="",
                    share_id="",
                    audience_type="owner",
                    category=reaction_category,
                    sentiment=reaction_sentiment,
                    importance=5 if reaction_category == "dealbreaker" else (4 if reaction_category == "question" else 3),
                    text=summary_text or f"Decision: {str(body.reaction or '').strip().lower()}",
                    source="workspace_property_feedback",
                    source_event_id=source_event_id,
                    decision_state=decision_state,
                    actor=actor,
                )
                structured_feedback_recorded += 1
            except Exception as exc:
                structured_feedback_errors.append(f"{structured_property_ref}: {type(exc).__name__}")
    result["structured_feedback_status"] = (
        "failed"
        if structured_feedback_errors and structured_feedback_recorded == 0
        else ("partial" if structured_feedback_errors else ("recorded" if structured_feedback_recorded else "not_attempted"))
    )
    result["structured_feedback_errors"] = structured_feedback_errors[:5]
    return PropertyFeedbackRecordOut(**result)


@router.post("/property/decisions", response_model=PropertyFeedbackRecordOut)
def record_property_decision(
    body: PropertyDecisionRecordIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyFeedbackRecordOut:
    service = build_product_service(container)
    actor = str(body.actor or context.operator_id or context.access_email or context.principal_id or "browser").strip()
    allowed_reason_keys = set(_property_feedback_reason_map().keys()) | set(_PROPERTY_DECISION_REASON_KEYS)
    normalized_reason_keys = [
        str(item or "").strip().lower()
        for item in list(body.reason_keys or [])
        if str(item or "").strip()
    ]
    invalid_reason_keys = [
        key
        for key in normalized_reason_keys
        if len(key) > 80 or key not in allowed_reason_keys
    ]
    if invalid_reason_keys:
        raise HTTPException(status_code=422, detail="invalid_property_feedback_reason_key")
    person_id = str(body.person_id or "self").strip() or "self"
    result = service.record_property_feedback(
        principal_id=context.principal_id,
        person_id=person_id,
        property_slug=body.property_slug,
        property_url=body.property_url,
        property_title=body.property_title,
        property_facts=dict(body.property_facts or {}),
        reaction=body.reaction,
        reason_keys=normalized_reason_keys,
        note=body.note,
        actor=actor,
    )
    persistence = dict(result.get("decision_persistence") or {})
    if persistence.get("persisted") is not True:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "property_decision_ledger_write_failed",
                "message": "The decision was not saved durably.",
                "persistence": persistence,
            },
        )
    result["structured_feedback_status"] = "not_attempted"
    result["structured_feedback_errors"] = []
    return PropertyFeedbackRecordOut(**result)


@router.get("/property/decisions", response_model=PropertyDecisionStateOut)
def property_decision_state(
    property_ref: str = Query(default="", max_length=500),
    limit: int = Query(default=50, ge=1, le=200),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyDecisionStateOut:
    service = build_product_service(container)
    result = service.property_decision_loop_state(
        principal_id=context.principal_id,
        property_ref=property_ref,
        limit=limit,
    )
    return PropertyDecisionStateOut(**result)


@router.patch("/property/agent-questions/{task_id}", response_model=PropertyDecisionStateOut)
def update_property_agent_question_task(
    task_id: str,
    body: PropertyAgentQuestionTaskUpdateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyDecisionStateOut:
    service = build_product_service(container)
    try:
        result = service.update_property_agent_question_task(
            principal_id=context.principal_id,
            task_id=task_id,
            status=str(body.status or "").strip().lower(),
            answer_source=str(body.answer_source or "").strip().lower(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PropertyDecisionStateOut(**result)


@router.patch("/property/documents/{document_id}", response_model=PropertyDecisionStateOut)
def update_property_document_record(
    document_id: str,
    body: PropertyDocumentRecordUpdateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyDecisionStateOut:
    service = build_product_service(container)
    try:
        result = service.update_property_document_record(
            principal_id=context.principal_id,
            document_id=document_id,
            verification_state=str(body.verification_state or "").strip().lower(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PropertyDecisionStateOut(**result)


@router.post("/property/decision-copilot", response_model=PropertyDecisionCopilotOut)
def property_decision_copilot(
    body: PropertyDecisionCopilotIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyDecisionCopilotOut:
    service = build_product_service(container)
    packet_service = build_fliplink_packet_service(container)
    property_ref = str(body.property_ref or body.property_url or body.property_title or "property").strip()
    feedback_summary = {}
    timeline_rows: list[dict[str, object]] = []
    change_rows: list[dict[str, object]] = []
    if property_ref:
        try:
            feedback_summary = dict(packet_service.feedback_summary(principal_id=context.principal_id, property_ref=property_ref))
        except Exception:
            feedback_summary = {}
        try:
            timeline_rows = list(packet_service.property_timeline(principal_id=context.principal_id, property_ref=property_ref))
        except Exception:
            timeline_rows = []
        try:
            change_rows = list(packet_service.property_change_log(principal_id=context.principal_id, property_ref=property_ref))
        except Exception:
            change_rows = []
    return PropertyDecisionCopilotOut(
        **service.property_decision_copilot(
            question=body.question,
            property_title=body.property_title,
            property_url=body.property_url,
            property_facts=dict(body.property_facts or {}),
            assessment=dict(body.assessment or {}),
            feedback_summary=feedback_summary,
            timeline_rows=timeline_rows,
            change_rows=change_rows,
            investment_context=[dict(row) for row in list(body.investment_context or []) if isinstance(row, dict)],
        )
    )


@router.post("/property/magic-fit-scenes", response_model=PropertyMagicFitSceneOut)
def create_property_magic_fit_scene(
    body: PropertyMagicFitSceneCreateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyMagicFitSceneOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        payload = service.create_property_magic_fit_scene(
            principal_id=context.principal_id,
            actor=actor,
            property_ref=body.property_ref,
            property_title=body.property_title,
            property_url=body.property_url,
            scene_type=body.scene_type,
            room_hint=body.room_hint,
            styling_hint=body.styling_hint,
            property_facts=dict(body.property_facts or {}),
            reference_urls=list(body.reference_urls or []),
            google_photos_session_id=body.google_photos_session_id,
            google_photos_account_email=body.google_photos_account_email,
            household_roles=list(body.household_roles or []),
            include_child_reference=body.include_child_reference,
            consent_personal_photos=body.consent_personal_photos,
            guardian_confirmed_for_children=body.guardian_confirmed_for_children,
            share_with_packet_pdf=body.share_with_packet_pdf,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PropertyMagicFitSceneOut(**payload)


@router.post("/property/magic-fit-reference-files", response_model=PropertyMagicFitReferenceUploadOut)
def upload_property_magic_fit_reference_files(
    body: PropertyMagicFitReferenceUploadIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyMagicFitReferenceUploadOut:
    items: list[PropertyMagicFitReferenceAssetOut] = []
    root = _magic_fit_reference_root(container, principal_id=context.principal_id)
    root.mkdir(parents=True, exist_ok=True)
    accepted = list(body.items or [])[:3]
    if not accepted:
        raise HTTPException(status_code=422, detail="property_magic_fit_reference_photo_required")
    for upload in accepted:
        mime_type = str(upload.mime_type or "").strip().lower()
        allowed_mime_types = {"image/jpeg": ("JPEG", ".jpg"), "image/png": ("PNG", ".png"), "image/webp": ("WEBP", ".webp")}
        if mime_type not in allowed_mime_types:
            raise HTTPException(status_code=422, detail="property_magic_fit_reference_image_required")
        data_url = str(upload.data_url or "").strip()
        match = re.match(r"^data:([^;,]+)?(?:;charset=[^;,]+)?;base64,(.+)$", data_url, re.IGNORECASE | re.DOTALL)
        if match is None:
            raise HTTPException(status_code=422, detail="property_magic_fit_reference_image_required")
        data_url_mime = str(match.group(1) or "").strip().lower()
        if data_url_mime and data_url_mime != mime_type:
            raise HTTPException(status_code=422, detail="property_magic_fit_reference_image_required")
        try:
            data = base64.b64decode(match.group(2), validate=False)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="property_magic_fit_reference_image_required") from exc
        if not data:
            raise HTTPException(status_code=422, detail="property_magic_fit_reference_image_required")
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="property_magic_fit_reference_file_too_large")
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                image.load()
                width, height = image.size
                if width <= 0 or height <= 0 or (width * height) > 24_000_000:
                    raise HTTPException(status_code=422, detail="property_magic_fit_reference_file_too_large")
                target_format, suffix = allowed_mime_types[mime_type]
                normalized_image = image.convert("RGB") if target_format in {"JPEG", "WEBP"} else image.convert("RGBA")
                output = BytesIO()
                save_kwargs: dict[str, object] = {}
                if target_format == "JPEG":
                    save_kwargs.update({"quality": 92, "optimize": True})
                normalized_image.save(output, format=target_format, **save_kwargs)
                data = output.getvalue()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail="property_magic_fit_reference_image_required") from exc
        reference_id = f"magicfitref_{uuid4().hex}"
        file_path = root / f"{reference_id}{suffix}"
        meta_path = root / f"{reference_id}.json"
        file_path.write_bytes(data)
        meta_path.write_text(
            json.dumps(
                {
                    "reference_id": reference_id,
                    "file_name": str(upload.file_name or "").strip()[:240],
                    "mime_type": mime_type[:120],
                    "size_bytes": len(data),
                    "file_name_on_disk": file_path.name,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        items.append(
            PropertyMagicFitReferenceAssetOut(
                reference_id=reference_id,
                file_name=str(upload.file_name or "").strip()[:240],
                mime_type=mime_type[:120],
                size_bytes=len(data),
                reference_url=f"/app/api/property/magic-fit-reference-files/{reference_id}",
            )
        )
    return PropertyMagicFitReferenceUploadOut(items=items)


@router.get("/property/magic-fit-reference-files/{reference_id}")
def get_property_magic_fit_reference_file(
    reference_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> FileResponse:
    root = _magic_fit_reference_root(container, principal_id=context.principal_id)
    meta_path = root / f"{_api_safe_token(reference_id)}.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="property_magic_fit_reference_not_found")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="property_magic_fit_reference_not_found") from exc
    file_name_on_disk = str(metadata.get("file_name_on_disk") or "").strip()
    file_path = root / file_name_on_disk
    if not file_name_on_disk or not file_path.exists():
        raise HTTPException(status_code=404, detail="property_magic_fit_reference_not_found")
    return FileResponse(
        file_path,
        media_type=str(metadata.get("mime_type") or "application/octet-stream"),
        filename=str(metadata.get("file_name") or file_path.name),
        headers={"Cache-Control": "private, max-age=3600", "X-Robots-Tag": "noindex, nofollow"},
    )


@public_router.get("/property/map-previews/{preview_id}.png")
def get_property_map_preview_file(
    preview_id: str,
    container: AppContainer = Depends(get_container),
) -> Response:
    safe_preview_id = _api_safe_token(preview_id, "preview")
    if not re.fullmatch(r"[0-9a-f]{40}", safe_preview_id):
        raise HTTPException(status_code=404, detail="property_map_preview_not_found")
    file_path = _property_map_preview_root(container) / f"{safe_preview_id}.png"
    if not file_path.is_file():
        return Response(
            _property_map_preview_missing_png(),
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, max-age=0, no-transform",
                "Content-Encoding": "identity",
                "X-Property-Map-Preview-State": "pending",
                "X-Property-Map-Preview-Label": _PROPERTY_MAP_PREVIEW_PENDING_LABEL,
                "X-Robots-Tag": "noindex, nofollow",
            },
        )
    try:
        content = file_path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="property_map_preview_not_found") from exc
    return Response(
        content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=86400, no-transform",
            "Content-Encoding": "identity",
            "X-Property-Map-Preview-State": "ready",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@router.get("/property/directories/brilliant-directories/members", include_in_schema=False)
def get_property_brilliant_directories_members(
    keyword: str = Query(default="", max_length=140),
    category: str = Query(default="", max_length=96),
    city: str = Query(default="", max_length=96),
    country_code: str = Query(default="", max_length=12),
    page: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=12, ge=1, le=50),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    del context
    try:
        config = brilliant_directories_service.load_brilliant_directories_config()
    except brilliant_directories_service.BrilliantDirectoriesApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if not config.configured:
        return {
            "contract_name": "propertyquarry.directory_projection.v1",
            "status": "disabled",
            "profile_count": 0,
            "profiles": [],
            "publication_allowed": False,
            "direct_property_truth_mutation_allowed": False,
        }
    try:
        packet = brilliant_directories_service.fetch_brilliant_directories_member_projection_packet(
            config,
            purpose="PropertyQuarry public directory member lookup",
            keyword=keyword,
            category=category,
            city=city,
            country_code=country_code,
            page=page,
            limit=limit,
        )
    except brilliant_directories_service.BrilliantDirectoriesApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    payload = packet.as_dict()
    return {
        "contract_name": "propertyquarry.directory_projection.v1",
        "status": "ready",
        "projection_mode": "public_directory_profile",
        "profile_count": payload.get("profile_count", 0),
        "profiles": payload.get("profiles", []),
        "publication_allowed": payload.get("publication_allowed", False),
        "direct_property_truth_mutation_allowed": payload.get("direct_property_truth_mutation_allowed", False),
    }


@router.get("/property/directories/members")
def get_property_directory_members(
    keyword: str = Query(default="", max_length=140),
    category: str = Query(default="", max_length=96),
    city: str = Query(default="", max_length=96),
    country_code: str = Query(default="", max_length=12),
    page: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=12, ge=1, le=50),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    del context
    try:
        config = brilliant_directories_service.load_brilliant_directories_config()
    except brilliant_directories_service.BrilliantDirectoriesApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if not config.configured:
        return {
            "contract_name": "propertyquarry.directory_projection.v1",
            "status": "disabled",
            "profile_count": 0,
            "profiles": [],
            "publication_allowed": False,
            "direct_property_truth_mutation_allowed": False,
        }
    try:
        packet = brilliant_directories_service.fetch_brilliant_directories_member_projection_packet(
            config,
            purpose="PropertyQuarry public directory member lookup",
            keyword=keyword,
            category=category,
            city=city,
            country_code=country_code,
            page=page,
            limit=limit,
        )
    except brilliant_directories_service.BrilliantDirectoriesApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    payload = packet.as_dict()
    return {
        "contract_name": "propertyquarry.directory_projection.v1",
        "status": "ready",
        "projection_mode": "public_directory_profile",
        "profile_count": payload.get("profile_count", 0),
        "profiles": payload.get("profiles", []),
        "publication_allowed": payload.get("publication_allowed", False),
        "direct_property_truth_mutation_allowed": payload.get("direct_property_truth_mutation_allowed", False),
    }


@router.get("/properties/{property_ref:path}/magic-fit-scene", response_model=PropertyMagicFitSceneOut | None)
def get_property_magic_fit_scene(
    property_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyMagicFitSceneOut | None:
    service = build_product_service(container)
    payload = service.latest_property_magic_fit_scene(
        principal_id=context.principal_id,
        property_ref=property_ref,
    )
    if not payload:
        return None
    return PropertyMagicFitSceneOut(**payload)


@router.post("/property-feedback")
def record_structured_property_feedback(
    body: StructuredPropertyFeedbackIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        feedback = service.record_structured_feedback(
            principal_id=context.principal_id,
            property_ref=body.property_ref,
            stakeholder_id=body.stakeholder_id,
            stakeholder_label=body.stakeholder_label,
            publication_id=body.publication_id,
            share_id=body.share_id,
            audience_type=body.audience_type,
            category=body.category,
            sentiment=body.sentiment,
            importance=body.importance,
            text=body.text,
            source=body.source,
            source_event_id=body.source_event_id,
            decision_state=body.decision_state,
            followup_status=body.followup_status,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "recorded", "feedback": feedback}


@router.post("/property-feedback/dadan")
def record_dadan_property_feedback(
    body: DadanPropertyFeedbackIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "dadan").strip()
    payload = body.model_dump()
    if not body.owner_review_confirmed:
        event = service._repo.record_event(
            {
                "publication_id": body.publication_id,
                "principal_id": context.principal_id,
                "event_type": "property_dadan_feedback_pending_review",
                "actor": actor,
                "payload_json": {
                    "property_ref": body.property_ref,
                    "publication_id": body.publication_id,
                    "share_id": body.share_id,
                    "audience_type": body.audience_type or "viewer",
                    "event_id": body.event_id,
                    "submission_id": body.submission_id,
                    "video_id": body.video_id,
                    "source": "dadan_video_feedback",
                    "trust_state": "untrusted_external",
                    "review_state": "pending_owner_review",
                },
            }
        )
        return {
            "status": "pending_owner_review",
            "source": "dadan_video_feedback",
            "property_ref": body.property_ref,
            "event_id": str(event.get("event_id") or ""),
            "recorded": [],
            "total": 0,
        }
    signals = dadan_feedback_signals(payload)
    recorded: list[dict[str, object]] = []
    for signal in signals:
        try:
            feedback = service.record_structured_feedback(
                principal_id=context.principal_id,
                property_ref=body.property_ref,
                stakeholder_id=signal.stakeholder_id,
                stakeholder_label=signal.stakeholder_label,
                publication_id=body.publication_id,
                share_id=body.share_id,
                audience_type=body.audience_type or "viewer",
                category=signal.category,
                sentiment=signal.sentiment,
                importance=signal.importance,
                text=signal.text,
                source="dadan_video_feedback",
                source_event_id=signal.source_event_id,
                decision_state=signal.decision_state,
                followup_status=signal.followup_status,
                actor=actor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        recorded.append(dict(feedback))
    return {
        "status": "recorded" if recorded else "no_signals",
        "source": "dadan_video_feedback",
        "property_ref": body.property_ref,
        "recorded": recorded,
        "total": len(recorded),
    }


@router.post("/property-video/requests/dadan")
def create_dadan_property_video_request(
    body: DadanVideoRequestCreateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = DadanVideoRequestService(repo=build_fliplink_packet_service(container)._repo)
    try:
        return service.create_recording_request(
            principal_id=context.principal_id,
            property_ref=body.property_ref,
            property_url=body.property_url,
            request_kind=body.request_kind,
            audience_type=body.audience_type,
            title=body.title,
            instructions=body.instructions,
            metadata=body.metadata,
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    except RuntimeError as exc:
        detail = str(exc or "dadan_request_failed")
        status_code = 409
        if detail in {"dadan_disabled", "dadan_api_key_required"}:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/integrations/heyy/whatsapp/channel")
def verify_heyy_whatsapp_channel(
    channel_id: str = "",
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = HeyyWhatsAppBridgeService(tool_runtime=container.tool_runtime)
    try:
        result = service.verify_channel(channel_id=channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "principal_id": context.principal_id,
    }


@router.get("/integrations/heyy/whatsapp/templates")
def list_heyy_whatsapp_templates(
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = HeyyWhatsAppBridgeService(tool_runtime=container.tool_runtime)
    try:
        result = service.list_templates()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "principal_id": context.principal_id,
    }


@router.post("/integrations/heyy/whatsapp/send-template")
def send_heyy_whatsapp_template(
    body: HeyyWhatsAppTemplateSendIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    try:
        require_heyy_enabled()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _require_heyy_send_allowed(container=container, principal_id=context.principal_id, phone_number=body.phone_number)
    service = HeyyWhatsAppBridgeService(tool_runtime=container.tool_runtime)
    try:
        result = service.send_template(
            phone_number=body.phone_number,
            template_id=body.template_id,
            channel_id=body.channel_id,
            variables=body.variables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "principal_id": context.principal_id,
    }


@router.post("/integrations/heyy/notifications/property-match")
def send_heyy_property_match_notification(
    body: HeyyPropertyMatchNotificationIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    try:
        require_heyy_enabled()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _require_heyy_send_allowed(container=container, principal_id=context.principal_id, phone_number=body.phone_number)
    service = HeyyWhatsAppBridgeService(tool_runtime=container.tool_runtime)
    variables = [
        {"name": "property_title", "value": body.property_title},
        {"name": "fit_score", "value": body.fit_score},
        {"name": "reason", "value": body.reason},
        {"name": "missing_fact", "value": body.missing_fact},
    ]
    try:
        result = service.send_template(
            phone_number=body.phone_number,
            template_id=body.template_id,
            channel_id=body.channel_id,
            variables=variables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    packet_service = build_fliplink_packet_service(container)
    event = packet_service._repo.record_event(  # noqa: SLF001
        {
            "publication_id": "",
            "principal_id": context.principal_id,
            "event_type": "heyy_whatsapp_template_sent",
            "actor": str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
            "payload_json": {
                "template_kind": "property_match",
                "property_ref": body.property_ref,
                "template_id": body.template_id,
                "channel_id": body.channel_id,
                **redact_phone_number(body.phone_number),
                "message_id": str(result.get("message_id") or "").strip(),
                "delivery_status": str(result.get("delivery_status") or "").strip(),
            },
        }
    )
    return {**result, "principal_id": context.principal_id, "event_id": str(event.get("event_id") or "")}


@router.post("/integrations/heyy/notifications/search-agent-digest")
def send_heyy_search_agent_digest_notification(
    body: HeyySearchDigestNotificationIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    try:
        require_heyy_enabled()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _require_heyy_send_allowed(container=container, principal_id=context.principal_id, phone_number=body.phone_number)
    service = HeyyWhatsAppBridgeService(tool_runtime=container.tool_runtime)
    variables = [
        {"name": "agent_name", "value": body.agent_name},
        {"name": "homes_checked", "value": body.homes_checked},
        {"name": "ranked_count", "value": body.ranked_count},
        {"name": "top_fit_score", "value": body.top_fit_score},
        {"name": "held_back_count", "value": body.held_back_count},
    ]
    try:
        result = service.send_template(
            phone_number=body.phone_number,
            template_id=body.template_id,
            channel_id=body.channel_id,
            variables=variables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    packet_service = build_fliplink_packet_service(container)
    event = packet_service._repo.record_event(  # noqa: SLF001
        {
            "publication_id": "",
            "principal_id": context.principal_id,
            "event_type": "heyy_whatsapp_template_sent",
            "actor": str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
            "payload_json": {
                "template_kind": "search_agent_digest",
                "search_agent_id": body.search_agent_id,
                "template_id": body.template_id,
                "channel_id": body.channel_id,
                **redact_phone_number(body.phone_number),
                "message_id": str(result.get("message_id") or "").strip(),
                "delivery_status": str(result.get("delivery_status") or "").strip(),
            },
        }
    )
    return {**result, "principal_id": context.principal_id, "event_id": str(event.get("event_id") or "")}


@router.post("/property-feedback/{feedback_id}/followup-status")
def update_property_feedback_followup_status(
    feedback_id: str,
    body: PropertyFeedbackStatusUpdateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    try:
        event = service.update_feedback_followup_status(
            principal_id=context.principal_id,
            feedback_id=feedback_id,
            followup_status=body.followup_status,
            note=body.note,
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"event": dict(event)}


@router.get("/property-feedback")
def list_structured_property_feedback(
    property_ref: str = Query(default=""),
    stakeholder_id: str = Query(default=""),
    publication_id: str = Query(default=""),
    category: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=50),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    all_items = service.list_structured_feedback(
        principal_id=context.principal_id,
        property_ref=property_ref,
        stakeholder_id=stakeholder_id,
        publication_id=publication_id,
        category=category,
    )
    items = list(all_items)[:limit]
    return {"items": items, "total": len(all_items)}


@router.post("/property-feedback/cluster")
def cluster_structured_property_feedback(
    property_ref: str = Query(min_length=1),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    return {"property_ref": property_ref, "clusters": service.cluster_feedback(principal_id=context.principal_id, property_ref=property_ref)}


@router.get("/stakeholders/{stakeholder_id}/preferences")
def get_stakeholder_property_preferences(
    stakeholder_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    return service.stakeholder_preferences(principal_id=context.principal_id, stakeholder_id=stakeholder_id)


@router.get("/properties/{property_ref:path}/feedback-summary")
def get_property_feedback_summary(
    property_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    return service.feedback_summary(principal_id=context.principal_id, property_ref=property_ref)


@router.get("/property/notifications/preview")
def get_property_notification_preview(
    template: str = Query(min_length=1),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    del container, context
    try:
        return property_notification_preview(template)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/property-summaries/generate")
def generate_property_summary_artifact(
    body: PropertySummaryGenerateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    try:
        artifact = service.generate_summary_artifact(
            principal_id=context.principal_id,
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            artifact_type=body.artifact_type,
            audience_type=body.audience_type,
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"artifact": artifact}


@router.post("/property/opportunities/{candidate_ref}/generate")
def generate_property_opportunity_artifact(
    candidate_ref: str,
    body: PropertyOpportunityGenerateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    product = build_product_service(container)
    run = product.get_property_search_run_status(
        principal_id=context.principal_id,
        run_id=body.run_id,
    )
    if not isinstance(run, dict) or not run:
        raise HTTPException(status_code=404, detail="property_search_run_not_found")
    candidate = find_property_search_candidate(run, candidate_ref=candidate_ref)
    if candidate is None:
        raise HTTPException(status_code=404, detail="property_opportunity_not_found")

    summary = dict(run.get("summary") or {}) if isinstance(run.get("summary"), dict) else {}
    existing_opportunity = (
        dict(candidate.get("opportunity") or {})
        if isinstance(candidate.get("opportunity"), dict)
        else {}
    )
    person_id = str(
        existing_opportunity.get("person_id")
        or run.get("opportunity_person_id")
        or summary.get("opportunity_person_id")
        or run.get("preference_person_id")
        or summary.get("preference_person_id")
        or "self"
    ).strip() or "self"
    search_preferences = next(
        (
            dict(value)
            for value in (
                run.get("property_search_preferences"),
                run.get("preferences"),
            )
            if isinstance(value, dict) and value
        ),
        {},
    )
    projection = product._materialize_property_search_opportunities(
        principal_id=context.principal_id,
        person_id=person_id,
        run_id=body.run_id,
        sources=[{"top_candidates": [candidate]}],
        search_preferences=search_preferences,
    )
    opportunity = (
        dict(candidate.get("opportunity") or {})
        if isinstance(candidate.get("opportunity"), dict)
        else {}
    )
    opportunity = property_opportunity_public_projection(opportunity)
    if (
        str(opportunity.get("status") or "").strip() != "ready"
        or not str(opportunity.get("opportunity_id") or "").strip()
    ):
        raise HTTPException(status_code=503, detail="property_opportunity_persistence_unavailable")

    actor = str(
        context.operator_id
        or context.access_email
        or context.principal_id
        or "browser"
    ).strip()
    summary_service = build_fliplink_packet_service(container)
    try:
        artifact = summary_service.generate_summary_artifact(
            principal_id=context.principal_id,
            subject_type="property",
            subject_id=str(candidate.get("candidate_ref") or candidate_ref).strip(),
            artifact_type=body.artifact_type,
            audience_type=body.audience_type,
            actor=actor,
            context_json={
                "title": str(candidate.get("title") or "Property opportunity").strip(),
                "opportunity": opportunity,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "status": "ready",
        "opportunity": opportunity,
        "opportunity_projection": projection,
        "artifact": artifact,
        "generation": {
            "provider": "PropertyQuarry",
            "mode": "local_opportunity_brief",
            "basis": "durable_preference_assessment",
            "artifact_status": "ready",
        },
        "publication": {
            "mode": "local_only",
            "scope": "private_artifact",
            "status": "not_published",
            "external_provider": "",
            "external_publication_verified": False,
            "external_receipt_ref": "",
        },
    }


@router.post("/property/opportunities/{candidate_ref}/generate-cover")
def generate_property_opportunity_concept_cover(
    candidate_ref: str,
    body: PropertyOpportunityConceptCoverIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    product = build_product_service(container)
    run = product.get_property_search_run_status(
        principal_id=context.principal_id,
        run_id=body.run_id,
    )
    if not isinstance(run, dict) or not run:
        raise HTTPException(status_code=404, detail="property_search_run_not_found")
    candidate = find_property_search_candidate(run, candidate_ref=candidate_ref)
    if candidate is None:
        raise HTTPException(status_code=404, detail="property_opportunity_not_found")
    opportunity = property_opportunity_public_projection(candidate.get("opportunity"))
    opportunity_id = str(opportunity.get("opportunity_id") or "").strip()
    if str(opportunity.get("status") or "") != "ready" or not opportunity_id:
        raise HTTPException(
            status_code=503,
            detail="property_opportunity_persistence_unavailable",
        )
    actor = str(
        context.operator_id
        or context.access_email
        or context.principal_id
        or "browser"
    ).strip()
    try:
        generation = product.enqueue_property_opportunity_concept_cover(
            principal_id=context.principal_id,
            run_id=body.run_id,
            candidate_ref=str(candidate.get("candidate_ref") or candidate_ref).strip(),
            opportunity_id=opportunity_id,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        **generation,
        "opportunity_id": opportunity_id,
        "provider": "1min.AI",
        "action": "image_generate",
        "execution_mode": "durable_worker",
        "publication": {
            "scope": "private_generated_asset",
            "status": "not_published",
            "external_publication_verified": False,
        },
    }


@router.get("/property/opportunities/generations/{generation_id}")
def get_property_opportunity_concept_cover(
    generation_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    generation = (
        build_product_service(container).get_property_opportunity_concept_cover(
            principal_id=context.principal_id,
            generation_id=generation_id,
        )
    )
    if generation is None:
        raise HTTPException(
            status_code=404,
            detail="property_opportunity_generation_not_found",
        )
    return generation


@router.get("/property-summaries/{artifact_id}")
def get_property_summary_artifact(
    artifact_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    artifact = service.get_summary_artifact(principal_id=context.principal_id, artifact_id=artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="property_summary_artifact_not_found")
    return {"artifact": artifact}


@router.get("/properties/{property_ref:path}/change-log")
def get_property_change_log(
    property_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    return {"items": service.property_change_log(principal_id=context.principal_id, property_ref=property_ref)}


@router.get("/stakeholders/{stakeholder_id}/timeline")
def get_property_stakeholder_timeline(
    stakeholder_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    items = service.stakeholder_timeline(principal_id=context.principal_id, stakeholder_id=stakeholder_id)
    return {"items": items, "total": len(items)}


@router.get("/properties/{property_ref:path}/timeline")
def get_property_timeline(
    property_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    items = service.property_timeline(principal_id=context.principal_id, property_ref=property_ref)
    return {"items": items, "total": len(items)}


@router.post("/followups/{followup_id}/assign")
def assign_property_followup(
    followup_id: str,
    body: FollowupAssignIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    try:
        event = service.assign_followup(
            principal_id=context.principal_id,
            followup_id=followup_id,
            owner=body.owner,
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"event": dict(event)}


@router.post("/followups/{followup_id}/resolve")
def resolve_property_followup(
    followup_id: str,
    body: FollowupResolveIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    try:
        event = service.resolve_followup(
            principal_id=context.principal_id,
            followup_id=followup_id,
            resolution=body.resolution,
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"event": dict(event)}


@router.get("/offers")
def list_property_offers(
    property_ref: str = Query(default=""),
    publication_id: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    items = service.list_offers(
        principal_id=context.principal_id,
        property_ref=property_ref,
        publication_id=publication_id,
    )
    return {"items": items, "total": len(items)}


@router.post("/offers/{offer_id}/checkout")
def start_property_offer_checkout(
    offer_id: str,
    property_ref: str = Query(default=""),
    publication_id: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_fliplink_packet_service(container)
    try:
        return service.start_offer_checkout(
            principal_id=context.principal_id,
            offer_id=offer_id,
            property_ref=property_ref,
            publication_id=publication_id,
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/people/{person_id}/preference-profile/teable-projection", response_model=dict[str, list[dict[str, object]]])
def get_preference_teable_projection(
    person_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, list[dict[str, object]]]:
    service = build_product_service(container)
    return service.preference_teable_projection_records(
        principal_id=context.principal_id,
        person_id=person_id,
    )


@router.get("/people/{person_id}/preference-profile/teable-projection-summary", response_model=dict[str, object])
def get_preference_teable_projection_summary(
    person_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    return service.preference_teable_projection_summary(
        principal_id=context.principal_id,
        person_id=person_id,
    )


@router.get("/people/{person_id}/preference-profile/teable-sync-preview", response_model=dict[str, object])
def get_preference_teable_sync_preview(
    person_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    return service.preference_teable_sync_preview(
        principal_id=context.principal_id,
        person_id=person_id,
    )


@router.post("/people/{person_id}/preference-profile/teable-sync", response_model=dict[str, object])
def request_preference_teable_sync(
    person_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    return service.request_preference_teable_sync(
        principal_id=context.principal_id,
        person_id=person_id,
    )


@router.get("/property/teable-projection", response_model=dict[str, list[dict[str, object]]])
def get_propertyquarry_teable_projection(
    run_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, list[dict[str, object]]]:
    service = build_product_service(container)
    return service.propertyquarry_teable_projection_records(
        principal_id=context.principal_id,
        run_id=run_id,
        limit=limit,
    )


@router.get("/property/teable-projection-summary", response_model=dict[str, object])
def get_propertyquarry_teable_projection_summary(
    run_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    return service.propertyquarry_teable_projection_summary(
        principal_id=context.principal_id,
        run_id=run_id,
        limit=limit,
    )


@router.get("/property/teable-sync-preview", response_model=dict[str, object])
def get_propertyquarry_teable_sync_preview(
    run_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    return service.propertyquarry_teable_sync_preview(
        principal_id=context.principal_id,
        run_id=run_id,
        limit=limit,
    )


@router.post("/property/teable-sync", response_model=dict[str, object])
def request_propertyquarry_teable_sync(
    run_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    return service.request_propertyquarry_teable_sync(
        principal_id=context.principal_id,
        run_id=run_id,
        limit=limit,
    )


@router.get("/handoffs", response_model=list[HandoffNoteOut])
def list_handoffs(
    status: str | None = Query(default="pending"),
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[HandoffNoteOut]:
    service = build_product_service(container)
    return [
        handoff_out(item)
        for item in service.list_handoffs(
            principal_id=context.principal_id,
            limit=limit,
            operator_id=str(context.operator_id or "").strip(),
            status=status,
        )
    ]


@router.get("/handoffs/{handoff_ref:path}/history", response_model=list[HandoffAssignmentHistoryOut])
def get_handoff_history(
    handoff_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> list[HandoffAssignmentHistoryOut]:
    service = build_product_service(container)
    found = service.get_handoff(principal_id=context.principal_id, handoff_ref=handoff_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="handoff_not_found")
    human_task_id = found.id.split(":", 1)[1] if found.id.startswith("human_task:") else found.id
    rows = container.orchestrator.list_human_task_assignment_history(
        human_task_id,
        principal_id=context.principal_id,
        limit=limit,
    )
    return [handoff_assignment_history_out(row) for row in rows]


@router.get("/handoffs/{handoff_ref:path}", response_model=HandoffNoteOut)
def get_handoff(
    handoff_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HandoffNoteOut:
    service = build_product_service(container)
    found = service.get_handoff(principal_id=context.principal_id, handoff_ref=handoff_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="handoff_not_found")
    return handoff_out(found)


@router.post("/handoffs/{handoff_ref:path}/assign", response_model=HandoffNoteOut)
def assign_handoff(
    handoff_ref: str,
    body: HandoffAssignIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HandoffNoteOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or body.operator_id).strip()
    updated = service.assign_handoff(
        principal_id=context.principal_id,
        handoff_ref=handoff_ref,
        operator_id=body.operator_id,
        actor=actor,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="handoff_not_assignable")
    return handoff_out(updated)


@router.post("/handoffs/{handoff_ref:path}/complete", response_model=HandoffNoteOut)
def complete_handoff(
    handoff_ref: str,
    body: HandoffCompleteIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HandoffNoteOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or body.operator_id).strip()
    updated = service.complete_handoff(
        principal_id=context.principal_id,
        handoff_ref=handoff_ref,
        operator_id=body.operator_id,
        actor=actor,
        resolution=body.resolution,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="handoff_not_completable")
    return handoff_out(updated)


@router.post("/handoffs/{handoff_ref:path}/retry-send", response_model=HandoffNoteOut)
def retry_handoff_send(
    handoff_ref: str,
    body: HandoffAssignIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HandoffNoteOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or body.operator_id).strip()
    try:
        updated = service.retry_delivery_followup_send(
            principal_id=context.principal_id,
            handoff_ref=handoff_ref,
            operator_id=body.operator_id,
            actor=actor,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="handoff_not_retryable")
    return handoff_out(updated)


@router.post("/handoffs/{handoff_ref:path}/recreate", response_model=HandoffNoteOut)
def recreate_handoff(
    handoff_ref: str,
    body: HandoffAssignIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HandoffNoteOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or body.operator_id).strip()
    try:
        updated = service.recreate_property_tour_followup(
            principal_id=context.principal_id,
            handoff_ref=handoff_ref,
            operator_id=body.operator_id,
            actor=actor,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="handoff_not_recreatable")
    return handoff_out(updated)


@router.get("/evidence", response_model=EvidenceResponse)
def list_evidence(
    limit: int = Query(default=40, ge=1, le=200),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> EvidenceResponse:
    service = build_product_service(container)
    items = service.list_evidence(
        principal_id=context.principal_id,
        limit=limit,
        operator_id=str(context.operator_id or "").strip(),
    )
    return EvidenceResponse(generated_at=now_iso(), items=[evidence_item_out(item) for item in items], total=len(items))


@router.get("/evidence/{evidence_ref:path}", response_model=EvidenceItemOut)
def get_evidence(
    evidence_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> EvidenceItemOut:
    service = build_product_service(container)
    found = service.get_evidence(
        principal_id=context.principal_id,
        evidence_ref=evidence_ref,
        operator_id=str(context.operator_id or "").strip(),
    )
    if found is None:
        raise HTTPException(status_code=404, detail="evidence_not_found")
    return evidence_item_out(found)


@router.get("/rules", response_model=RuleResponse)
def list_rules(
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RuleResponse:
    service = build_product_service(container)
    items = service.list_rules(principal_id=context.principal_id)
    return RuleResponse(generated_at=now_iso(), items=[rule_out(item) for item in items], total=len(items))


@router.get("/rules/{rule_id:path}", response_model=RuleItemOut)
def get_rule(
    rule_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RuleItemOut:
    service = build_product_service(container)
    found = service.get_rule(principal_id=context.principal_id, rule_id=rule_id)
    if found is None:
        raise HTTPException(status_code=404, detail="rule_not_found")
    return rule_out(found)


@router.post("/rules/{rule_id:path}/simulate", response_model=RuleItemOut)
def simulate_rule(
    rule_id: str,
    body: RuleSimulateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RuleItemOut:
    service = build_product_service(container)
    found = service.simulate_rule(principal_id=context.principal_id, rule_id=rule_id, proposed_value=body.proposed_value)
    if found is None:
        raise HTTPException(status_code=404, detail="rule_not_found")
    return rule_out(found)


@router.get("/invitations", response_model=WorkspaceInvitationResponse)
def get_workspace_invitations(
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=200),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceInvitationResponse:
    service = build_product_service(container)
    items = service.list_workspace_invitations(principal_id=context.principal_id, status=status, limit=limit)
    return WorkspaceInvitationResponse(
        generated_at=now_iso(),
        items=[WorkspaceInvitationOut(**item) for item in items],
        total=len(items),
    )


@router.post("/invitations", response_model=WorkspaceInvitationOut)
def create_workspace_invitation(
    body: WorkspaceInvitationCreateIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceInvitationOut:
    _require_operator_for_workspace_role(role=body.role, context=context)
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "workspace").strip()
    payload = service.create_workspace_invitation(
        principal_id=context.principal_id,
        email=body.email,
        role=body.role,
        invited_by=actor,
        display_name=body.display_name,
        note=body.note,
        expires_in_days=body.expires_in_days,
        base_url=str(request.base_url),
    )
    return WorkspaceInvitationOut(**payload)


@router.post("/invitations/accept", response_model=WorkspaceInvitationOut)
def accept_workspace_invitation(
    body: WorkspaceInvitationAcceptIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceInvitationOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "workspace").strip()
    try:
        payload = service.accept_workspace_invitation(
            token=body.token,
            accepted_by=actor,
            display_name=body.display_name,
            operator_id=body.operator_id,
        )
    except ValueError as exc:
        if str(exc or "").strip() == "operator_seat_limit_reached":
            raise HTTPException(status_code=409, detail="operator_seat_limit_reached") from exc
        raise
    if payload is None:
        raise HTTPException(status_code=404, detail="workspace_invitation_not_found")
    return WorkspaceInvitationOut(**payload)


@router.post("/access-sessions", response_model=WorkspaceAccessSessionOut)
def create_workspace_access_session(
    body: WorkspaceAccessSessionCreateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceAccessSessionOut:
    _require_operator_for_workspace_role(role=body.role, operator_id=body.operator_id, context=context)
    service = build_product_service(container)
    payload = service.issue_workspace_access_session(
        principal_id=context.principal_id,
        email=body.email,
        role=body.role,
        display_name=body.display_name,
        operator_id=body.operator_id,
        source_kind="workspace_access_api",
        expires_in_hours=body.expires_in_hours,
    )
    return WorkspaceAccessSessionOut(**payload)


def _normalize_workspace_access_legacy_payload(*, body: dict[str, object]) -> tuple[str, str, str, str, int, str]:
    normalized_email = str(body.get("email") or body.get("recipient_email") or "").strip().lower()
    if not normalized_email:
        raise HTTPException(status_code=400, detail="recipient_email_required")
    expires_raw = str(body.get("expires_in_hours") or 72).strip()
    try:
        expires_in_hours = int(expires_raw)
    except ValueError:
        expires_in_hours = 72
    return (
        normalized_email,
        str(body.get("role") or "operator").strip().lower() or "operator",
        str(body.get("display_name") or "").strip(),
        str(body.get("operator_id") or "").strip(),
        max(1, expires_in_hours),
        str(body.get("return_to") or body.get("default_target") or "").strip(),
    )


@router.post("/workspace-access", include_in_schema=False, response_model=WorkspaceAccessSessionOut)
def create_workspace_access_session_legacy(
    body: dict[str, object],
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceAccessSessionOut:
    service = build_product_service(container)
    email, role, display_name, operator_id, expires_in_hours, default_target = _normalize_workspace_access_legacy_payload(
        body=dict(body or {})
    )
    _require_operator_for_workspace_role(role=role, operator_id=operator_id, context=context)
    payload = service.issue_workspace_access_session(
        principal_id=context.principal_id,
        email=email,
        role=role,
        display_name=display_name,
        operator_id=operator_id,
        source_kind="workspace_access_api",
        expires_in_hours=expires_in_hours,
        default_target=default_target,
    )
    return WorkspaceAccessSessionOut(**payload)


@router.get("/access-sessions", response_model=WorkspaceAccessSessionResponse)
def list_workspace_access_sessions(
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceAccessSessionResponse:
    service = build_product_service(container)
    items = service.list_workspace_access_sessions(
        principal_id=context.principal_id,
        status=status,
        limit=limit,
    )
    return WorkspaceAccessSessionResponse(
        generated_at=now_iso(),
        items=[WorkspaceAccessSessionOut(**item) for item in items],
        total=len(items),
    )


@router.post("/access-sessions/{session_id}/revoke", response_model=WorkspaceAccessSessionOut)
def revoke_workspace_access_session(
    session_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceAccessSessionOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "workspace").strip()
    payload = service.revoke_workspace_access_session(
        principal_id=context.principal_id,
        session_id=session_id,
        actor=actor,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="workspace_access_session_not_found")
    return WorkspaceAccessSessionOut(**payload)


@router.post("/invitations/{invitation_id}/revoke", response_model=WorkspaceInvitationOut)
def revoke_workspace_invitation(
    invitation_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WorkspaceInvitationOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "workspace").strip()
    payload = service.revoke_workspace_invitation(
        principal_id=context.principal_id,
        invitation_id=invitation_id,
        actor=actor,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="workspace_invitation_not_found")
    return WorkspaceInvitationOut(**payload)


@router.get("/people/{person_id}/detail/history", response_model=list[HistoryEntryOut])
def get_person_detail_history(
    person_id: str,
    *,
    context: RequestContext = Depends(get_request_context),
    container: AppContainer = Depends(get_container),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[HistoryEntryOut]:
    service = build_product_service(container)
    person = service.get_person(principal_id=context.principal_id, person_id=person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person_not_found")
    return [HistoryEntryOut(**value.__dict__) for value in service.get_person_history(principal_id=context.principal_id, person_id=person_id, limit=limit)]

@router.get("/commitment-candidates/{candidate_id}", response_model=CommitmentCandidateOut)
def get_commitment_candidate(
    candidate_id: str,
    *,
    context: RequestContext = Depends(get_request_context),
    container: AppContainer = Depends(get_container),
) -> CommitmentCandidateOut:
    service = build_product_service(container)
    found = service.get_commitment_candidate(principal_id=context.principal_id, candidate_id=candidate_id)
    if found is None:
        raise HTTPException(status_code=404, detail="commitment_candidate_not_found")
    return commitment_candidate_out(found)
