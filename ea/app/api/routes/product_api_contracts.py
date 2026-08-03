from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.domain.models import ExecutionEvent
from app.domain.property_preference_events import (
    PREFERENCE_EVENT_FEEDBACK_ACCEPTED,
    PREFERENCE_EVENT_FEEDBACK_REJECTED,
    PREFERENCE_OBJECT_FEEDBACK,
    PROPERTY_PREFERENCE_DOMAIN,
)
from app.product.models import BriefItem, CommitmentCandidate, CommitmentItem, DeadlineItem, DecisionItem, DecisionQueueItem, DraftCandidate, EvidenceItem, EvidenceRef, HandoffNote, HistoryEntry, PersonDetail, PersonProfile, RuleItem, ThreadItem


class StrictMutationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_bounded_json_value(
    value: object,
    *,
    depth: int = 0,
    max_depth: int = 6,
    max_items: int = 128,
    max_string_length: int = 20_000,
) -> object:
    if depth > max_depth:
        raise ValueError("nested_payload_depth_exceeds_limit")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        normalized = float(value)
        if not math.isfinite(normalized) or abs(normalized) > 1_000_000_000_000:
            raise ValueError("nested_numeric_value_exceeds_limit")
        return value
    if isinstance(value, str):
        if len(value) > max_string_length:
            raise ValueError("nested_string_value_exceeds_limit")
        return value
    if isinstance(value, list):
        if len(value) > max_items:
            raise ValueError("nested_list_items_exceed_limit")
        for item in value:
            _validate_bounded_json_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
            )
        return value
    if isinstance(value, dict):
        if len(value) > max_items:
            raise ValueError("nested_object_keys_exceed_limit")
        for key, item in value.items():
            if len(str(key or "")) > 160:
                raise ValueError("nested_object_key_exceeds_limit")
            _validate_bounded_json_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
            )
        return value
    raise ValueError("nested_payload_value_invalid")


class EvidenceRefOut(BaseModel):
    ref_id: str
    label: str
    href: str = ""
    source_type: str = ""
    note: str = ""


class BriefItemOut(BaseModel):
    id: str
    workspace_id: str
    kind: str
    title: str
    summary: str
    score: float
    why_now: str
    evidence_refs: list[EvidenceRefOut]
    related_people: list[str]
    related_commitment_ids: list[str]
    recommended_action: str
    status: str
    confidence: float = 0.0
    object_ref: str = ""
    profile_followup_refs: list[str] = Field(default_factory=list)
    evidence_count: int = 0


class DecisionQueueItemOut(BaseModel):
    id: str
    queue_kind: str
    title: str
    summary: str
    priority: str
    rank_score: float = 0.0
    deadline: str | None = None
    owner_role: str = ""
    requires_principal: bool = False
    evidence_refs: list[EvidenceRefOut]
    profile_followup_refs: list[str] = Field(default_factory=list)
    resolution_state: str


class DecisionItemOut(BaseModel):
    id: str
    title: str
    summary: str
    priority: str
    owner_role: str
    due_at: str | None = None
    status: str
    decision_type: str = ""
    recommendation: str = ""
    next_action: str = ""
    rationale: str = ""
    options: list[str]
    evidence_refs: list[EvidenceRefOut]
    related_commitment_ids: list[str]
    linked_thread_ids: list[str]
    related_people: list[str]
    impact_summary: str = ""
    sla_status: str = ""
    resolution_reason: str = ""


class DeadlineOut(BaseModel):
    id: str
    title: str
    summary: str
    priority: str
    start_at: str | None = None
    end_at: str | None = None
    status: str


class CommitmentOut(BaseModel):
    id: str
    source_type: str
    source_ref: str
    statement: str
    owner: str
    counterparty: str
    due_at: str | None = None
    status: str
    last_activity_at: str | None = None
    risk_level: str
    proof_refs: list[EvidenceRefOut]
    confidence: float = 0.5
    channel_hint: str = ""
    resolution_code: str = ""
    resolution_reason: str = ""
    duplicate_of_ref: str = ""
    merged_into_ref: str = ""
    merged_from_refs: list[str] = []


WalkthroughProviderChoice = Literal["", "mootion", "magicfit", "omagic", "magic"]


class CommitmentCandidateOut(BaseModel):
    candidate_id: str = ""
    title: str
    details: str
    source_text: str
    confidence: float
    suggested_due_at: str | None = None
    counterparty: str = ""
    channel_hint: str = ""
    source_ref: str = ""
    signal_type: str = ""
    status: str = "pending"
    kind: str = "commitment"
    stakeholder_id: str = ""
    duplicate_of_ref: str = ""
    merge_strategy: str = "create"


class DraftCandidateOut(BaseModel):
    id: str
    thread_ref: str
    recipient_summary: str
    intent: str
    draft_text: str
    tone: str
    requires_approval: bool
    approval_status: str
    provenance_refs: list[EvidenceRefOut]
    send_channel: str


class PersonProfileOut(BaseModel):
    id: str
    display_name: str
    role_or_company: str
    importance_score: int
    relationship_temperature: str
    open_loops_count: int
    latest_touchpoint_at: str | None = None
    preferred_tone: str
    themes: list[str]
    risks: list[str]


class HandoffNoteOut(BaseModel):
    id: str
    queue_item_ref: str
    summary: str
    owner: str
    due_time: str | None = None
    escalation_status: str
    status: str
    task_type: str = ""
    resolution: str = ""
    draft_ref: str = ""
    recipient_email: str = ""
    subject: str = ""
    delivery_reason: str = ""
    property_url: str = ""
    listing_id: str = ""
    variant_key: str = ""
    blocked_reason: str = ""
    tour_url: str = ""
    open_tour_url: str = ""
    vendor_tour_url: str = ""
    source_virtual_tour_url: str = ""
    generated_reconstruction_url: str = ""
    layout_preview_url: str = ""
    layout_preview_status: str = ""
    editor_url: str = ""
    connector_binding_id: str = ""
    source_ref: str = ""
    external_id: str = ""
    evidence_refs: list[EvidenceRefOut]


class HandoffAssignmentHistoryOut(BaseModel):
    event_id: str
    human_task_id: str
    event_name: str
    assignment_state: str
    assigned_operator_id: str
    assignment_source: str
    assigned_at: str | None = None
    assigned_by_actor_id: str
    resolution: str
    created_at: str


class HistoryEntryOut(BaseModel):
    event_type: str
    created_at: str | None = None
    source_id: str = ""
    actor: str = ""
    detail: str = ""


class PersonDetailOut(BaseModel):
    profile: PersonProfileOut
    commitments: list[CommitmentOut]
    drafts: list[DraftCandidateOut]
    threads: list[ThreadItemOut]
    queue_items: list[DecisionQueueItemOut]
    handoffs: list[HandoffNoteOut]
    evidence_refs: list[EvidenceRefOut]
    history: list[HistoryEntryOut]


class ThreadItemOut(BaseModel):
    id: str
    title: str
    channel: str
    status: str
    last_activity_at: str | None = None
    summary: str
    counterparties: list[str]
    draft_ids: list[str]
    related_commitment_ids: list[str]
    related_decision_ids: list[str]
    evidence_refs: list[EvidenceRefOut]


class EvidenceItemOut(BaseModel):
    id: str
    label: str
    source_type: str
    summary: str
    href: str = ""
    related_object_refs: list[str]


class RuleItemOut(BaseModel):
    id: str
    label: str
    scope: str
    status: str
    summary: str
    current_value: str
    impact: str
    requires_approval: bool = False
    simulated_effect: str = ""


class OfficeEventOut(BaseModel):
    observation_id: str
    channel: str
    event_type: str
    created_at: str
    source_id: str = ""
    external_id: str = ""
    summary: str = ""
    object_refs: list[str] = Field(default_factory=list)
    payload: dict[str, object] = Field(default_factory=dict)


class GroundingActionOut(BaseModel):
    label: str
    href: str = ""
    method: str = "get"


class GroundingSourceOut(BaseModel):
    label: str
    path: str = ""
    as_of: str = ""


class GroundingPackOut(BaseModel):
    id: str
    title: str
    summary: str
    bullets: list[str] = Field(default_factory=list)
    actions: list[GroundingActionOut] = Field(default_factory=list)
    sources: list[GroundingSourceOut] = Field(default_factory=list)


class WorkspaceDiagnosticsOut(BaseModel):
    workspace: dict[str, object]
    selected_channels: list[str]
    plan: dict[str, object]
    billing: dict[str, object]
    entitlements: dict[str, object]
    commercial: dict[str, object]
    readiness: dict[str, object]
    operators: dict[str, object]
    providers: dict[str, object]
    queue_health: dict[str, object]
    product_control: dict[str, object] = Field(default_factory=dict)
    support_verification: dict[str, object] = Field(default_factory=dict)
    usage: dict[str, int]
    analytics: dict[str, object]


class WorkspacePlanDetailOut(BaseModel):
    workspace: dict[str, object]
    selected_channels: list[str]
    plan: dict[str, object]
    billing: dict[str, object]
    entitlements: dict[str, object]
    commercial: dict[str, object]
    operators: dict[str, object]


class WorkspaceUsageDetailOut(BaseModel):
    workspace: dict[str, object]
    selected_channels: list[str]
    usage: dict[str, int]
    analytics: dict[str, object]
    readiness: dict[str, object]
    operators: dict[str, object]


class WorkspaceOutcomesOut(BaseModel):
    generated_at: str
    time_to_first_value_seconds: int | None = None
    first_value_event: str = ""
    memo_open_rate: float = 0.0
    approval_coverage_rate: float = 0.0
    approval_action_rate: float = 0.0
    delivery_followup_closeout_count: int = 0
    delivery_followup_blocked_count: int = 0
    delivery_followup_resolution_rate: float | None = None
    delivery_followup_blocked_rate: float | None = None
    commitment_close_rate: float = 0.0
    correction_rate: float = 0.0
    churn_risk: str = "watch"
    success_summary: str = ""
    memo_loop: dict[str, object] = Field(default_factory=dict)
    office_loop_proof: dict[str, object] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)


class WorkspaceTrustOut(BaseModel):
    generated_at: str
    health_score: int = 0
    workspace_summary: str = ""
    readiness: dict[str, str] = Field(default_factory=dict)
    provider_posture: dict[str, object] = Field(default_factory=dict)
    reliability: dict[str, str] = Field(default_factory=dict)
    audit_retention: str = "standard"
    evidence_count: int = 0
    rule_count: int = 0
    recent_events: list[OfficeEventOut] = Field(default_factory=list)
    public_help_grounding: GroundingPackOut | None = None


class WorkspaceSupportBundleOut(BaseModel):
    workspace: dict[str, object]
    selected_channels: list[str]
    plan: dict[str, object]
    billing: dict[str, object]
    entitlements: dict[str, object]
    commercial: dict[str, object]
    readiness: dict[str, object]
    product_control: dict[str, object] = Field(default_factory=dict)
    support_verification: dict[str, object] = Field(default_factory=dict)
    usage: dict[str, object]
    analytics: dict[str, object]
    approvals: dict[str, object]
    human_tasks: list[dict[str, object]]
    providers: dict[str, object]
    queue_health: dict[str, object]
    assignment_suggestions: list[dict[str, object]]
    pending_delivery: list[dict[str, object]]
    recent_events: list[OfficeEventOut] = Field(default_factory=list)
    support_assistant_grounding: GroundingPackOut | None = None


class OperatorCenterLaneOut(BaseModel):
    key: str
    label: str
    state: str = "clear"
    count: int = 0
    detail: str = ""
    href: str = ""


class OperatorCenterActionOut(BaseModel):
    label: str
    detail: str = ""
    href: str = ""
    action_href: str = ""
    action_label: str = ""
    action_value: str = ""
    action_method: str = ""
    return_to: str = ""
    secondary_action_href: str = ""
    secondary_action_label: str = ""
    secondary_action_value: str = ""
    secondary_action_method: str = ""
    secondary_return_to: str = ""
    tertiary_action_href: str = ""
    tertiary_action_label: str = ""
    tertiary_action_value: str = ""
    tertiary_action_method: str = ""
    tertiary_return_to: str = ""
    quaternary_action_href: str = ""
    quaternary_action_label: str = ""
    quaternary_action_value: str = ""
    quaternary_action_method: str = ""
    quaternary_return_to: str = ""


class OperatorCenterOut(BaseModel):
    generated_at: str
    workspace: dict[str, object]
    operators: dict[str, object]
    queue_health: dict[str, object]
    providers: dict[str, object]
    readiness: dict[str, object]
    delivery: dict[str, object]
    access: dict[str, object]
    sync: dict[str, object]
    usage: dict[str, int]
    lanes: list[OperatorCenterLaneOut] = Field(default_factory=list)
    next_actions: list[OperatorCenterActionOut] = Field(default_factory=list)
    recent_runtime: list[dict[str, object]] = Field(default_factory=list)
    snapshot: dict[str, int] = Field(default_factory=dict)
    operator_memo_grounding: GroundingPackOut | None = None


class WorkspaceInvitationOut(BaseModel):
    invitation_id: str
    email: str
    role: str = "operator"
    display_name: str = ""
    note: str = ""
    status: str = "pending"
    invited_by: str = ""
    invited_at: str = ""
    expires_at: str = ""
    accepted_at: str = ""
    accepted_by: str = ""
    revoked_at: str = ""
    invite_url: str = ""
    invite_token: str = ""
    operator_id: str = ""
    access_token: str = ""
    access_url: str = ""
    access_expires_at: str = ""
    email_delivery_status: str = ""
    email_delivery_error: str = ""
    email_message_id: str = ""
    email_provider: str = ""


class WorkspaceInvitationResponse(BaseModel):
    generated_at: str
    items: list[WorkspaceInvitationOut]
    total: int


class ChannelLoopItemOut(BaseModel):
    title: str
    detail: str
    tag: str
    href: str = ""
    object_ref: str = ""
    profile_followup_refs: list[str] = Field(default_factory=list)
    recommended_action: str = ""
    action_href: str = ""
    action_label: str = ""
    action_method: str = "get"
    secondary_action_href: str = ""
    secondary_action_label: str = ""
    secondary_action_method: str = "get"
    tertiary_action_href: str = ""
    tertiary_action_label: str = ""
    tertiary_action_method: str = "get"
    quaternary_action_href: str = ""
    quaternary_action_label: str = ""
    quaternary_action_method: str = "get"


class ChannelDigestOut(BaseModel):
    key: str
    headline: str
    summary: str
    preview_text: str
    items: list[ChannelLoopItemOut]
    stats: dict[str, int]


class ChannelLoopOut(BaseModel):
    headline: str
    summary: str
    items: list[ChannelLoopItemOut]
    stats: dict[str, int]
    digests: list[ChannelDigestOut] = []


class BriefResponse(BaseModel):
    generated_at: str
    items: list[BriefItemOut]
    total: int


class QueueResponse(BaseModel):
    generated_at: str
    items: list[DecisionQueueItemOut]
    total: int


class DecisionResponse(BaseModel):
    generated_at: str
    items: list[DecisionItemOut]
    total: int


class DeadlineResponse(BaseModel):
    generated_at: str
    items: list[DeadlineOut]
    total: int


class ThreadResponse(BaseModel):
    generated_at: str
    items: list[ThreadItemOut]
    total: int


class EvidenceResponse(BaseModel):
    generated_at: str
    items: list[EvidenceItemOut]
    total: int


class RuleResponse(BaseModel):
    generated_at: str
    items: list[RuleItemOut]
    total: int


class OfficeEventResponse(BaseModel):
    generated_at: str
    items: list[OfficeEventOut]
    total: int


class SearchResultOut(BaseModel):
    id: str
    kind: str
    title: str
    summary: str = ""
    href: str = ""
    score: float = 0.0
    secondary_label: str = ""
    related_object_refs: list[str] = Field(default_factory=list)
    action_href: str = ""
    action_label: str = ""
    action_method: str = ""
    action_value: str = ""


class SearchResponse(BaseModel):
    generated_at: str
    items: list[SearchResultOut]
    total: int


class WebhookOut(BaseModel):
    webhook_id: str
    label: str
    target_url: str
    status: str = "active"
    event_types: list[str] = Field(default_factory=list)
    created_at: str = ""
    last_delivery_at: str = ""
    delivery_count: int = 0


class WebhookDeliveryOut(BaseModel):
    delivery_id: str
    webhook_id: str
    label: str = ""
    target_url: str = ""
    matched_event_type: str = ""
    delivery_kind: str = "event"
    status: str = "queued"
    created_at: str = ""
    source_id: str = ""
    summary: str = ""
    payload: dict[str, object] = Field(default_factory=dict)


class WebhookResponse(BaseModel):
    generated_at: str
    items: list[WebhookOut]
    total: int


class WebhookDeliveryResponse(BaseModel):
    generated_at: str
    items: list[WebhookDeliveryOut]
    total: int


class WorkspaceInvitationCreateIn(BaseModel):
    email: str = Field(min_length=3)
    role: str = "operator"
    display_name: str = ""
    note: str = ""
    expires_in_days: int = 14


class WorkspaceInvitationAcceptIn(BaseModel):
    token: str = Field(min_length=8)
    display_name: str = ""
    operator_id: str = ""


class WorkspaceAccessSessionCreateIn(BaseModel):
    email: str = Field(min_length=3)
    role: str = "principal"
    display_name: str = ""
    operator_id: str = ""
    expires_in_hours: int = 72


class WorkspaceAccessSessionOut(BaseModel):
    session_id: str
    principal_id: str
    email: str = ""
    role: str = "principal"
    display_name: str = ""
    operator_id: str = ""
    source_kind: str = ""
    issued_at: str = ""
    status: str = "active"
    revoked_at: str = ""
    revoked_by: str = ""
    expires_at: str = ""
    access_token: str = ""
    access_url: str = ""
    default_target: str = "/app/properties"


class WorkspaceAccessSessionResponse(BaseModel):
    generated_at: str
    items: list[WorkspaceAccessSessionOut]
    total: int


class ChannelDigestDeliveryCreateIn(BaseModel):
    recipient_email: str = Field(min_length=3)
    role: str = "principal"
    display_name: str = ""
    operator_id: str = ""
    delivery_channel: str = "email"
    expires_in_hours: int = 72


class ChannelDigestDeliveryOut(BaseModel):
    delivery_id: str
    digest_key: str
    principal_id: str
    recipient_email: str
    role: str = "principal"
    display_name: str = ""
    operator_id: str = ""
    delivery_channel: str = "email"
    expires_at: str = ""
    delivery_token: str = ""
    delivery_url: str = ""
    open_url: str = ""
    access_session_id: str = ""
    access_token: str = ""
    access_url: str = ""
    default_target: str = "/app/properties"
    headline: str = ""
    preview_text: str = ""
    plain_text: str = ""
    email_delivery_status: str = ""
    email_delivery_error: str = ""
    email_message_id: str = ""
    email_provider: str = ""
    telegram_delivery_status: str = ""
    telegram_delivery_error: str = ""
    telegram_message_ids: list[str] = Field(default_factory=list)
    telegram_chat_ref: str = ""


class DraftApproveIn(BaseModel):
    reason: str = "Approved from product draft queue."


class QueueResolveIn(BaseModel):
    action: str = Field(min_length=1)
    reason: str = ""
    reason_code: str = ""
    due_at: str | None = None


class CommitmentCreateIn(BaseModel):
    title: str = Field(min_length=1)
    details: str = ""
    due_at: str | None = None
    priority: str = "medium"
    counterparty: str = ""
    owner: str = "office"
    kind: str = "commitment"
    stakeholder_id: str = ""
    channel_hint: str = "email"


class CommitmentExtractIn(BaseModel):
    text: str = Field(min_length=1)
    counterparty: str = ""
    due_at: str | None = None


class CommitmentCandidateStageIn(BaseModel):
    text: str = Field(min_length=1)
    counterparty: str = ""
    due_at: str | None = None
    kind: str = "commitment"
    stakeholder_id: str = ""


class CommitmentCandidateReviewIn(BaseModel):
    reviewer: str = Field(min_length=1)
    title: str = ""
    details: str = ""
    due_at: str | None = None
    counterparty: str = ""
    kind: str = ""
    stakeholder_id: str = ""


class HandoffAssignIn(BaseModel):
    operator_id: str = Field(min_length=1)


class HandoffCompleteIn(BaseModel):
    operator_id: str = Field(min_length=1)
    resolution: str = "completed"


class WorkspaceMorningMemoSettingsIn(BaseModel):
    workspace_name: str = ""
    language: str = "en"
    timezone: str = "Europe/Vienna"
    enabled: bool = False
    cadence: str = "daily_morning"
    recipient_email: str = ""
    delivery_time_local: str = "08:00"
    quiet_hours_start: str = "20:00"
    quiet_hours_end: str = "07:00"


class PersonCorrectionIn(BaseModel):
    preferred_tone: str = ""
    add_theme: str = ""
    remove_theme: str = ""
    add_risk: str = ""
    remove_risk: str = ""


_PROPERTY_PREFERENCE_DOMAINS = frozenset(
    {
        "willhaben",
        PROPERTY_PREFERENCE_DOMAIN,
        "propertyquarry",
        "immmo",
        "derstandard",
        "immoscout_de",
        "immowelt",
        "immobilienscout24",
        "kleinanzeigen",
        "rightmove",
        "zoopla",
        "idealista",
        "fotocasa",
        "justiz_auction",
        "forced_auction",
        "genossenschaften",
        "cooperative_housing",
    }
)
_GENERAL_PREFERENCE_DOMAINS = frozenset({"general"})
_PREFERENCE_DOMAINS = _PROPERTY_PREFERENCE_DOMAINS | _GENERAL_PREFERENCE_DOMAINS
_PREFERENCE_STRENGTHS = frozenset({"low", "medium", "high"})
_PREFERENCE_SOURCE_MODES = frozenset(
    {
        "explicit",
        "explicit_correction",
        "explicit_feedback",
        "manual",
        "behavioral_inference",
        "conversation_inference",
    }
)
_PREFERENCE_STATUSES = frozenset({"active", "inactive"})
_PREFERENCE_DECAY_POLICIES = frozenset({"manual_only", "reinforce_only", "decay_on_disconfirm", "none"})
_PREFERENCE_EVIDENCE_EVENT_TYPES = frozenset(
    {
        "document_pattern_detected",
        PREFERENCE_EVENT_FEEDBACK_ACCEPTED,
        PREFERENCE_EVENT_FEEDBACK_REJECTED,
        "filter_applied",
        "listing_rejected",
        "listing_saved",
        "listing_shortlisted",
        "listing_viewed",
        "manual_correction",
        "preference_feedback",
        "profile_followup_nudged",
        "profile_followup_resolution_recorded",
        "property_feedback",
        "public_tour_external_feedback",
        "search_result_opened",
        "source_scan_result_reviewed",
    }
)
_PREFERENCE_EVIDENCE_OBJECT_TYPES = frozenset(
    {
        "conversation",
        "document",
        PREFERENCE_OBJECT_FEEDBACK,
        "listing",
        "manual_note",
        "profile",
        "property",
        "property_listing",
        "property_search",
        "search_run",
        "source_scan_result",
        "tour",
    }
)
_PROPERTY_PREFERENCE_VALUE_SPECS = {
    ("constraint", "max_total_rent_eur"): "positive_number",
    ("constraint", "min_rooms"): "positive_number",
    ("constraint", "min_area_sqm"): "positive_number",
    ("constraint", "require_floorplan"): "bool",
    ("constraint", "require_360"): "bool",
    ("constraint", "require_lift"): "bool",
    ("constraint", "require_quiet_micro_location"): "bool",
    ("soft_preference", "preferred_areas"): "text_list",
    ("soft_preference", "requires_floorplan_for_remote_review"): "bool",
    ("soft_preference", "prefer_balcony"): "bool",
    ("soft_preference", "prefer_outdoor_space"): "bool",
    ("soft_preference", "prefer_lift"): "bool",
    ("soft_preference", "prefer_360_for_remote_review"): "bool",
    ("soft_preference", "prefer_subway_nearby"): "bool",
    ("soft_preference", "prefer_supermarket_nearby"): "bool",
    ("soft_preference", "prefer_pharmacy_nearby"): "bool",
    ("soft_preference", "prefer_playgrounds_nearby"): "bool",
    ("soft_preference", "prefer_libraries_nearby"): "bool",
    ("soft_preference", "prefer_markets_nearby"): "bool",
    ("soft_preference", "prefer_hardware_store_nearby"): "bool",
    ("soft_preference", "prefer_shopping_street_nearby"): "bool",
    ("soft_preference", "prefer_shopping_center_nearby"): "bool",
    ("soft_preference", "prefer_public_pool_nearby"): "bool",
    ("soft_preference", "prefer_theatre_nearby"): "bool",
    ("soft_preference", "prefer_medical_care_nearby"): "bool",
    ("soft_preference", "prefer_low_crime_area"): "bool",
    ("soft_preference", "prefer_good_air_quality"): "bool",
    ("soft_preference", "prefer_air_conditioning"): "bool",
    ("soft_preference", "prefer_attic_apartment"): "bool",
    ("soft_preference", "prefer_low_parking_pressure"): "bool",
    ("soft_preference", "prefer_high_quality_drinking_water"): "bool",
    ("soft_preference", "prefer_quiet_micro_location"): "bool",
    ("soft_preference", "prefer_bike_infrastructure"): "bool",
    ("soft_preference", "prefer_running_green_space"): "bool",
    ("soft_preference", "prefer_unlimited_lease"): "bool",
    ("soft_preference", "prefer_lower_total_rent_eur"): "positive_number",
    ("soft_preference", "min_area_sqm_preference"): "positive_number",
    ("aversion", "avoid_heating_types"): "text_list",
    ("aversion", "avoided_areas"): "text_list",
    ("aversion", "avoid_attic_apartment"): "bool",
}
_GENERAL_PREFERENCE_VALUE_SPECS = {
    ("decision_style", "needs_side_by_side_comparison"): "bool",
    ("workflow_preference", "prefers_written_follow_up"): "bool",
    ("workflow_preference", "prefers_concise_updates"): "bool",
    ("workflow_preference", "prefers_direct_followups"): "bool",
}

def _canonical_property_preference_key(key: object) -> str:
    return _normalized_preference_text(key, max_length=120)


def _preference_error(code: str) -> PydanticCustomError:
    return PydanticCustomError(code, code)


def _normalized_preference_text(value: object, *, max_length: int = 80) -> str:
    text = " ".join(str(value or "").split()).strip().lower()
    if len(text) > max_length:
        raise _preference_error("preference_text_too_long")
    return text


def _normalize_preference_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "ja", "on"}:
            return True
        if lowered in {"0", "false", "no", "nein", "off"}:
            return False
    raise _preference_error("preference_value_must_be_boolean")


def _normalize_preference_positive_number(value: object) -> int | float:
    if isinstance(value, bool):
        raise _preference_error("preference_value_must_be_positive_number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", "."))
        except ValueError as exc:
            raise _preference_error("preference_value_must_be_positive_number") from exc
    else:
        raise _preference_error("preference_value_must_be_positive_number")
    if number <= 0 or number > 10_000_000:
        raise _preference_error("preference_value_out_of_range")
    return int(number) if number.is_integer() else number


def _normalize_preference_text_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items: list[object] = value.split(",") if "," in value else [value]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raise _preference_error("preference_value_must_be_text_list")
    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        if isinstance(raw, (dict, list, tuple, set)):
            raise _preference_error("preference_value_must_be_flat_text_list")
        text = " ".join(str(raw or "").split()).strip()
        if not text:
            continue
        if len(text) > 120:
            raise _preference_error("preference_value_item_too_long")
        marker = text.lower()
        if marker in seen:
            continue
        seen.add(marker)
        items.append(text)
    if not items:
        raise _preference_error("preference_value_must_not_be_empty")
    if len(items) > 25:
        raise _preference_error("preference_value_too_many_items")
    return items


def _preference_value_specs_for_domain(domain: str) -> dict[tuple[str, str], str]:
    if domain in _GENERAL_PREFERENCE_DOMAINS:
        return _GENERAL_PREFERENCE_VALUE_SPECS
    return _PROPERTY_PREFERENCE_VALUE_SPECS


def _normalize_preference_node_value(*, domain: str, category: str, key: str, value: object) -> object:
    specs = _preference_value_specs_for_domain(domain)
    value_kind = specs.get((category, key))
    if value_kind is None:
        raise _preference_error("unsupported_preference_node")
    if value_kind == "bool":
        return _normalize_preference_bool(value)
    if value_kind == "positive_number":
        return _normalize_preference_positive_number(value)
    if value_kind == "text_list":
        return _normalize_preference_text_list(value)
    raise _preference_error("unsupported_preference_node")


def _validate_preference_json_size(value: object) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        raise _preference_error("preference_value_must_be_json_serializable") from exc
    if len(encoded.encode("utf-8")) > 4096:
        raise _preference_error("preference_value_too_large")


class PreferenceProfileUpsertIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    profile_scope: str | None = Field(default=None, max_length=80)
    consent_mode: str | None = Field(default=None, max_length=80)
    learning_enabled: bool | None = None
    high_stakes_domains_enabled: bool | None = None


class PreferenceMailboxImportIn(BaseModel):
    account_email: str = Field(default="", max_length=320)
    consent_confirmed: bool = False
    consent_note: str = Field(default="", max_length=500)
    email_limit: int = Field(default=80, ge=1, le=250)
    lookback_days: int = Field(default=365, ge=7, le=3650)

    @field_validator("account_email", mode="before")
    @classmethod
    def _normalize_account_email(cls, value: object) -> str:
        return " ".join(str(value or "").split()).strip().lower()

    @field_validator("consent_note", mode="before")
    @classmethod
    def _normalize_consent_note(cls, value: object) -> str:
        return " ".join(str(value or "").split()).strip()


class PreferenceNodeUpsertIn(BaseModel):
    domain: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=120)
    value_json: object
    strength: str = "medium"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_mode: str = "explicit"
    status: str = "active"
    decay_policy: str = "reinforce_only"

    @field_validator("domain", "category", "key", "strength", "source_mode", "status", "decay_policy", mode="before")
    @classmethod
    def _normalize_tokens(cls, value: object) -> str:
        return _normalized_preference_text(value, max_length=120)

    @model_validator(mode="after")
    def _validate_preference_node(self) -> "PreferenceNodeUpsertIn":
        self.key = _canonical_property_preference_key(self.key)
        if self.domain not in _PREFERENCE_DOMAINS:
            raise _preference_error("unsupported_preference_domain")
        if self.strength not in _PREFERENCE_STRENGTHS:
            raise _preference_error("invalid_preference_strength")
        if self.source_mode not in _PREFERENCE_SOURCE_MODES:
            raise _preference_error("invalid_preference_source_mode")
        if self.status not in _PREFERENCE_STATUSES:
            raise _preference_error("invalid_preference_status")
        if self.decay_policy not in _PREFERENCE_DECAY_POLICIES:
            raise _preference_error("invalid_preference_decay_policy")
        self.value_json = _normalize_preference_node_value(
            domain=self.domain,
            category=self.category,
            key=self.key,
            value=self.value_json,
        )
        _validate_preference_json_size(self.value_json)
        return self


class PreferenceNodeArchiveIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class PreferenceEvidenceEventIn(BaseModel):
    domain: str = Field(min_length=1, max_length=80)
    event_type: str = Field(min_length=1, max_length=120)
    object_type: str = Field(min_length=1, max_length=120)
    object_id: str = Field(min_length=1, max_length=500)
    source_ref: str = Field(default="", max_length=500)
    raw_signal_json: dict[str, object] = Field(default_factory=dict)
    interpreted_signal_json: dict[str, object] = Field(default_factory=dict)
    signal_strength: float = Field(default=0.5, ge=0.0, le=1.0)
    reversible: bool = True

    @field_validator("domain", "event_type", "object_type", mode="before")
    @classmethod
    def _normalize_tokens(cls, value: object) -> str:
        return _normalized_preference_text(value, max_length=120)

    @field_validator("object_id", "source_ref", mode="before")
    @classmethod
    def _normalize_refs(cls, value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @model_validator(mode="after")
    def _validate_preference_evidence(self) -> "PreferenceEvidenceEventIn":
        if self.domain not in _PREFERENCE_DOMAINS:
            raise _preference_error("unsupported_preference_domain")
        if self.event_type not in _PREFERENCE_EVIDENCE_EVENT_TYPES:
            raise _preference_error("unsupported_preference_evidence_event_type")
        if self.object_type not in _PREFERENCE_EVIDENCE_OBJECT_TYPES:
            raise _preference_error("unsupported_preference_evidence_object_type")
        _validate_preference_json_size(self.raw_signal_json)
        _validate_preference_json_size(self.interpreted_signal_json)
        return self


class PreferenceCorrectionApplyIn(BaseModel):
    domain: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=120)
    value_json: object
    strength: str = "high"
    reason: str = Field(default="", max_length=500)

    @field_validator("domain", "category", "key", "strength", mode="before")
    @classmethod
    def _normalize_tokens(cls, value: object) -> str:
        return _normalized_preference_text(value, max_length=120)

    @model_validator(mode="after")
    def _validate_preference_correction(self) -> "PreferenceCorrectionApplyIn":
        self.key = _canonical_property_preference_key(self.key)
        if self.domain not in _PREFERENCE_DOMAINS:
            raise _preference_error("unsupported_preference_domain")
        if self.strength not in _PREFERENCE_STRENGTHS:
            raise _preference_error("invalid_preference_strength")
        self.value_json = _normalize_preference_node_value(
            domain=self.domain,
            category=self.category,
            key=self.key,
            value=self.value_json,
        )
        _validate_preference_json_size(self.value_json)
        return self


class PreferenceDecisionAssessmentIn(BaseModel):
    domain: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    object_payload: dict[str, object] = Field(default_factory=dict)


class PropertyFeedbackSuggestionRequestIn(BaseModel):
    property_facts: dict[str, object] = Field(default_factory=dict)
    assessment: dict[str, object] = Field(default_factory=dict)


class PropertyFeedbackRecordIn(BaseModel):
    property_slug: str = Field(default="", max_length=200)
    property_url: str = Field(default="", max_length=2048)
    property_title: str = Field(default="", max_length=500)
    property_facts: dict[str, object] = Field(default_factory=dict)
    reaction: Literal["like", "dislike", "maybe", "hide"]
    reason_keys: list[str] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=2000)
    actor: str = Field(default="", max_length=200)


class PropertyDecisionRecordIn(PropertyFeedbackRecordIn):
    person_id: str = Field(default="self", max_length=120)


class PreferenceProfileSummaryOut(BaseModel):
    person_id: str
    principal_id: str
    display_name: str
    profile_scope: str
    consent_mode: str
    learning_enabled: bool
    high_stakes_domains_enabled: bool
    created_at: str
    updated_at: str


class PreferenceNodeOut(BaseModel):
    node_id: str
    principal_id: str
    person_id: str
    domain: str
    category: str
    key: str
    value_json: object
    strength: str
    confidence: float
    source_mode: str
    status: str
    decay_policy: str
    last_confirmed_at: str = ""
    last_observed_at: str = ""
    created_at: str
    updated_at: str


class PreferenceEvidenceEventOut(BaseModel):
    event_id: str
    principal_id: str
    person_id: str
    domain: str
    event_type: str
    object_type: str
    object_id: str
    source_ref: str = ""
    raw_signal_json: dict[str, object] = Field(default_factory=dict)
    interpreted_signal_json: dict[str, object] = Field(default_factory=dict)
    signal_strength: float
    reversible: bool
    recorded_at: str


class PreferenceDecisionAssessmentOut(BaseModel):
    assessment_id: str = ""
    principal_id: str = ""
    person_id: str = ""
    domain: str
    object_type: str
    object_id: str
    fit_score: float
    confidence: float
    predicted_reaction: str
    recommendation: str
    match_reasons_json: list[str] = Field(default_factory=list)
    mismatch_reasons_json: list[str] = Field(default_factory=list)
    unknowns_json: list[str] = Field(default_factory=list)
    blocking_constraints_json: list[str] = Field(default_factory=list)
    assessment_json: dict[str, object] = Field(default_factory=dict)
    generated_at: str = ""


class PreferenceCorrectionOut(BaseModel):
    correction_id: str
    principal_id: str
    person_id: str
    target_type: str
    target_id: str
    old_value_json: object
    new_value_json: object
    reason: str = ""
    corrected_by: str = ""
    corrected_at: str


class PreferenceEvidenceApplyOut(BaseModel):
    event: PreferenceEvidenceEventOut
    applied_nodes: list[PreferenceNodeOut] = Field(default_factory=list)


class PreferenceCorrectionApplyOut(BaseModel):
    node: PreferenceNodeOut
    correction: PreferenceCorrectionOut


class PreferenceProfileBundleOut(BaseModel):
    profile: PreferenceProfileSummaryOut
    preference_nodes: list[PreferenceNodeOut] = Field(default_factory=list)
    recent_evidence_events: list[PreferenceEvidenceEventOut] = Field(default_factory=list)
    recent_decision_assessments: list[PreferenceDecisionAssessmentOut] = Field(default_factory=list)
    recent_corrections: list[PreferenceCorrectionOut] = Field(default_factory=list)


class PreferenceMailboxImportActivityOut(BaseModel):
    source_ref: str = ""
    thread_id: str = ""
    account_email: str = ""
    activity_kind: str = ""
    provider: str = ""
    subject: str = ""
    location_hint: str = ""
    price_eur: float | None = None
    area_sqm: float | None = None
    rooms: float | None = None
    detected_features: list[str] = Field(default_factory=list)
    inferred_listing_mode: str = ""
    inferred_property_type: str = ""


class PreferenceMailboxImportOut(BaseModel):
    status: str
    person_id: str
    account_email: str = ""
    consent_confirmed: bool
    consent_note: str = ""
    imported_thread_total: int = 0
    activity_total: int = 0
    preregistration_total: int = 0
    inquiry_total: int = 0
    viewing_total: int = 0
    applied_nodes: list[PreferenceNodeOut] = Field(default_factory=list)
    activities: list[PreferenceMailboxImportActivityOut] = Field(default_factory=list)
    preference_snapshot: PreferenceProfileBundleOut
    teable_sync_status: str = ""
    teable_blocked_reason: str = ""


class PropertyFeedbackSuggestionOut(BaseModel):
    key: str
    label: str
    tone: str = ""
    explanation: str = ""


class PropertyFeedbackAgentQuestionOut(BaseModel):
    question: str
    status: str = "suggested"
    action: str = "ask_agent"


class PropertyFeedbackSuggestionSetOut(BaseModel):
    negative: list[PropertyFeedbackSuggestionOut] = Field(default_factory=list)
    positive: list[PropertyFeedbackSuggestionOut] = Field(default_factory=list)
    agent_questions: list[PropertyFeedbackAgentQuestionOut] = Field(default_factory=list)
    decision_consequences: list[str] = Field(default_factory=list)


class PropertyDecisionCopilotIn(StrictMutationIn):
    property_ref: str = Field(min_length=1, max_length=500)
    property_title: str = Field(default="", max_length=240)
    property_url: str = Field(default="", max_length=2000)
    question: str = Field(min_length=1, max_length=500)
    property_facts: dict[str, object] = Field(default_factory=dict, max_length=128)
    assessment: dict[str, object] = Field(default_factory=dict, max_length=128)
    investment_context: list[dict[str, object]] = Field(default_factory=list, max_length=24)

    @field_validator("property_facts", "assessment")
    @classmethod
    def _bounded_mapping(cls, value: dict[str, object]) -> dict[str, object]:
        _validate_bounded_json_value(value)
        return value

    @field_validator("investment_context")
    @classmethod
    def _bounded_investment_context(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        _validate_bounded_json_value(value, max_items=24)
        return value


class PropertyDecisionCopilotEvidenceOut(BaseModel):
    title: str
    detail: str
    confidence: str = ""
    source: str = ""


class PropertyDecisionCopilotActionOut(BaseModel):
    label: str
    action: str
    detail: str = ""
    reaction: str = ""
    reason_key: str = ""
    question: str = ""
    href: str = ""


class PropertyDecisionCopilotOut(BaseModel):
    name: str = "Clippy"
    mode: str = "property_decision_copilot"
    answer: str
    evidence: list[PropertyDecisionCopilotEvidenceOut] = Field(default_factory=list)
    actions: list[PropertyDecisionCopilotActionOut] = Field(default_factory=list)


class PropertyMagicFitSceneCreateIn(StrictMutationIn):
    property_ref: str = Field(min_length=1, max_length=500)
    property_title: str = Field(default="", max_length=240)
    property_url: str = Field(default="", max_length=2000)
    scene_type: str = Field(default="breakfast", max_length=80)
    room_hint: str = Field(default="", max_length=160)
    styling_hint: str = Field(default="", max_length=240)
    property_facts: dict[str, object] = Field(default_factory=dict, max_length=128)
    reference_urls: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list,
        max_length=6,
    )
    google_photos_session_id: str = Field(default="", max_length=200)
    google_photos_account_email: str = Field(default="", max_length=200)
    household_roles: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list,
        max_length=6,
    )
    include_child_reference: bool = False
    consent_personal_photos: bool = False
    guardian_confirmed_for_children: bool = False
    share_with_packet_pdf: bool = True
    note: str = Field(default="", max_length=500)

    @field_validator("property_facts")
    @classmethod
    def _bounded_property_facts(cls, value: dict[str, object]) -> dict[str, object]:
        _validate_bounded_json_value(value)
        return value

    @field_validator("reference_urls")
    @classmethod
    def _normalize_reference_urls(cls, value: list[str]) -> list[str]:
        cleaned = [str(item or "").strip() for item in list(value or []) if str(item or "").strip()]
        return cleaned[:6]


class PropertyMagicFitSceneOut(BaseModel):
    status: str = "created"
    scene_id: str
    property_ref: str
    property_title: str = ""
    scene_type: str
    room_hint: str = ""
    styling_hint: str = ""
    image_url: str = ""
    prompt: str = ""
    summary: str = ""
    reference_total: int = 0
    google_photos_session_id: str = ""
    household_roles: list[str] = Field(default_factory=list)
    visual_simulation: bool = True
    packet_pdf_enabled: bool = True
    consent_confirmed: bool = True
    generated_at: str = ""


class PropertyMagicFitReferenceAssetOut(BaseModel):
    reference_id: str
    file_name: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    reference_url: str = ""


class PropertyMagicFitReferenceUploadOut(BaseModel):
    status: str = "uploaded"
    items: list[PropertyMagicFitReferenceAssetOut] = Field(default_factory=list)


class PropertyMagicFitReferenceUploadItemIn(StrictMutationIn):
    file_name: str = Field(default="", max_length=240)
    mime_type: str = Field(default="", max_length=120)
    data_url: str = Field(min_length=1, max_length=12_000_000)


class PropertyMagicFitReferenceUploadIn(StrictMutationIn):
    items: list[PropertyMagicFitReferenceUploadItemIn] = Field(default_factory=list, max_length=3)


class PreferenceLearningFeedbackEventOut(BaseModel):
    event_type: str = ""
    recorded_at: str = ""
    reaction: str = ""
    reasons: list[str] = Field(default_factory=list)
    note: str = ""
    object_id: str = ""


class PreferenceLearningSummaryOut(BaseModel):
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    hard_rules: list[str] = Field(default_factory=list)
    recent_feedback: list[PreferenceLearningFeedbackEventOut] = Field(default_factory=list)


class PropertyFeedbackRecordOut(BaseModel):
    status: str
    reaction: str
    reason_keys: list[str] = Field(default_factory=list)
    evidence: PreferenceEvidenceApplyOut
    updated_assessment: PreferenceDecisionAssessmentOut
    learning_summary: PreferenceLearningSummaryOut
    preference_snapshot: PreferenceProfileBundleOut
    decision_ledger: dict[str, object] = Field(default_factory=dict)
    evidence_graph: list[dict[str, object]] = Field(default_factory=list)
    agent_question_tasks: list[dict[str, object]] = Field(default_factory=list)
    document_intake: list[dict[str, object]] = Field(default_factory=list)
    suppression_explanation: list[str] = Field(default_factory=list)
    decision_persistence: dict[str, object] = Field(default_factory=dict)
    structured_feedback_status: str = "not_attempted"
    structured_feedback_errors: list[str] = Field(default_factory=list)


class PropertyDecisionStateOut(BaseModel):
    status: str = "ready"
    property_ref: str = ""
    decisions: list[dict[str, object]] = Field(default_factory=list)
    evidence_claims: list[dict[str, object]] = Field(default_factory=list)
    agent_question_tasks: list[dict[str, object]] = Field(default_factory=list)
    document_intake: list[dict[str, object]] = Field(default_factory=list)
    latest_decision: dict[str, object] = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)
    persistence: dict[str, object] = Field(default_factory=dict)


class PropertyAgentQuestionTaskUpdateIn(BaseModel):
    status: str = Field(min_length=1, max_length=80)
    answer_source: str = Field(default="", max_length=120)


class PropertyDocumentRecordUpdateIn(BaseModel):
    verification_state: str = Field(min_length=1, max_length=80)


class RuleSimulateIn(BaseModel):
    proposed_value: str = Field(min_length=1)


class OfficeSignalIn(StrictMutationIn):
    signal_type: str = Field(min_length=1, max_length=120)
    channel: str = Field(default="office_api", max_length=120)
    title: str = Field(default="", max_length=500)
    summary: str = Field(default="", max_length=4_000)
    text: str = Field(default="", max_length=50_000)
    source_ref: str = Field(default="", max_length=2_000)
    external_id: str = Field(default="", max_length=500)
    counterparty: str = Field(default="", max_length=240)
    stakeholder_id: str = Field(default="", max_length=240)
    due_at: str | None = Field(default=None, max_length=80)
    payload: dict[str, object] = Field(default_factory=dict, max_length=128)

    @field_validator("payload")
    @classmethod
    def _bounded_payload(cls, value: dict[str, object]) -> dict[str, object]:
        _validate_bounded_json_value(value)
        return value


class OfficeSignalResultOut(BaseModel):
    observation_id: str
    channel: str
    event_type: str
    source_id: str = ""
    external_id: str = ""
    created_at: str
    staged_candidates: list[CommitmentCandidateOut] = Field(default_factory=list)
    staged_drafts: list[DraftCandidateOut] = Field(default_factory=list)
    staged_count: int = 0
    draft_count: int = 0
    deduplicated: bool = False
    ooda_loop: dict[str, object] = Field(default_factory=dict)
    attachment_imports: list[dict[str, object]] = Field(default_factory=list)


class WillhabenPropertyTourIn(StrictMutationIn):
    property_url: str = Field(min_length=1, max_length=2_000)
    request_kind: str = Field(default="tour", max_length=80)
    recipient_email: str = Field(default="", max_length=320)
    variant_key: str = Field(default="layout_first", max_length=120)
    binding_id: str = Field(default="", max_length=240)
    source_ref: str = Field(default="", max_length=500)
    external_id: str = Field(default="", max_length=500)
    run_id: str = Field(default="", max_length=160)
    candidate_ref: str = Field(default="", max_length=500)
    auto_deliver: bool = False
    allow_floorplan_only: bool = False
    diorama_style_hint: str = Field(default="", max_length=240)
    walkthrough_provider_key: WalkthroughProviderChoice = Field(
        default="",
        description="Preferred walkthrough provider lane. `mootion`, `magicfit`, and `omagic` are choosable; `magic` is its alias.",
    )
    external_processing_consent_granted: StrictBool = Field(
        default=False,
        description="Explicit consent for this authenticated principal's private walkthrough request to cross the governed external-processing lane.",
    )


class WillhabenPropertyTourOut(BaseModel):
    generated_at: str
    status: str
    property_url: str
    title: str = ""
    listing_id: str = ""
    variant_key: str = ""
    artifact_id: str = ""
    execution_session_id: str = ""
    connector_binding_id: str = ""
    diorama_style_hint: str = ""
    tour_url: str = ""
    open_tour_url: str = ""
    verified_tour_url: str = ""
    generated_reconstruction_url: str = ""
    layout_preview_url: str = ""
    layout_preview_status: str = ""
    vendor_tour_url: str = ""
    source_virtual_tour_url: str = ""
    generated_reconstruction_url: str = ""
    layout_preview_url: str = ""
    layout_preview_status: str = ""
    editor_url: str = ""
    delivery_email: str = ""
    delivery_status: str = ""
    telegram_delivery_status: str = ""
    telegram_delivery_error: str = ""
    telegram_message_ids: list[str] = Field(default_factory=list)
    telegram_chat_ref: str = ""
    telegram_video_delivery_status: str = ""
    telegram_video_delivery_error: str = ""
    telegram_video_message_ids: list[str] = Field(default_factory=list)
    telegram_video_url: str = ""
    telegram_video_followup_ref: str = ""
    blocked_reason: str = ""
    failure_stage: str = ""
    error_code: str = ""
    diagnostic_sha256: str = ""
    human_task_id: str = ""
    source_ref: str = ""
    external_id: str = ""
    request_kind: str = "tour"
    run_id: str = ""
    candidate_ref: str = ""
    flythrough_url: str = ""
    flythrough_status: str = ""
    flythrough_reason: str = ""
    tour_status: str = ""
    upstream_tour_status: str = ""
    status_label: str = ""
    status_detail: str = ""
    eta_label: str = ""
    progress_pct: int = 0
    poll_after_seconds: int = 0
    tour_media_mode: str = ""
    personal_fit_assessment: dict[str, object] = Field(default_factory=dict)


class SignalIngestEndpointCreateIn(BaseModel):
    label: str = "Pocket signal ingest"
    signal_type: str = "saved_link"
    counterparty: str = "Pocket"


class SignalIngestEndpointOut(BaseModel):
    endpoint_id: str
    label: str
    channel: str = "pocket"
    signal_type: str = "saved_link"
    counterparty: str = ""
    created_at: str = ""
    upload_url: str
    ingest_token: str = ""


class PocketSignalImportIn(StrictMutationIn):
    path: str = Field(min_length=1, max_length=4_096)
    counterparty: str = Field(default="Pocket", max_length=240)


class PocketSignalImportOut(BaseModel):
    generated_at: str
    source_path: str
    source_formats: list[str] = Field(default_factory=list)
    items: list[OfficeSignalResultOut] = Field(default_factory=list)
    total: int = 0
    synced_total: int = 0
    deduplicated_total: int = 0
    suppressed_total: int = 0
    parsed_entry_total: int = 0


class GoogleLocationHistoryImportIn(StrictMutationIn):
    path: str = Field(min_length=1, max_length=4_096)


class GoogleLocationHistoryImportOut(BaseModel):
    generated_at: str
    source_path: str
    source_formats: list[str] = Field(default_factory=list)
    imported_total: int = 0
    deduplicated_total: int = 0
    matched_recording_total: int = 0
    unmatched_recording_total: int = 0
    indexed_recording_total: int = 0
    updated_metadata_total: int = 0


class GoogleLocationHistoryConnectStartOut(BaseModel):
    provider_key: str
    principal_id: str
    requested_scopes: list[str] = Field(default_factory=list)
    auth_url: str
    state: str


class GoogleLocationHistoryConnectCallbackOut(BaseModel):
    provider_key: str
    principal_id: str
    binding_id: str
    google_email: str = ""
    google_subject: str = ""
    granted_scopes: list[str] = Field(default_factory=list)
    token_status: str = ""


class GoogleLocationHistorySyncOut(BaseModel):
    generated_at: str
    provider_key: str
    google_email: str = ""
    state: str = ""
    archive_job_id: str = ""
    imported_total: int = 0
    matched_recording_total: int = 0
    unmatched_recording_total: int = 0
    indexed_recording_total: int = 0
    updated_metadata_total: int = 0


class NoneverbiaSignalImportIn(StrictMutationIn):
    path: str = Field(min_length=1, max_length=4_096)
    counterparty: str = Field(default="Noneverbia", max_length=240)


class NoneverbiaSignalImportOut(BaseModel):
    generated_at: str
    source_path: str
    source_formats: list[str] = Field(default_factory=list)
    items: list[OfficeSignalResultOut] = Field(default_factory=list)
    total: int = 0
    synced_total: int = 0
    deduplicated_total: int = 0
    suppressed_total: int = 0
    parsed_entry_total: int = 0
    preference_evidence_total: int = 0
    preference_evidence_applied_total: int = 0


class PocketSignalSyncOut(BaseModel):
    generated_at: str
    mode: str = "incremental"
    items: list[OfficeSignalResultOut] = Field(default_factory=list)
    total: int = 0
    synced_total: int = 0
    deduplicated_total: int = 0
    suppressed_total: int = 0
    failed_total: int = 0
    recording_total: int = 0
    staging_suppressed_total: int = 0
    archived_total: int = 0
    archive_dismissed_total: int = 0
    archive_failed_total: int = 0
    teable_index_status: str = ""
    teable_index_blocked_reason: str = ""
    teable_index_row_total: int = 0
    teable_index_sync_attempted: bool = False
    preference_evidence_total: int = 0
    preference_evidence_applied_total: int = 0
    assistant_trigger_total: int = 0
    assistant_trigger_executed_total: int = 0
    assistant_trigger_blocked_total: int = 0
    cursor_used: bool = True
    cursor_persisted: bool = True
    cursor_updated_at: str = ""
    cursor_recording_id: str = ""
    cursor_advanced: bool = False
    scan_truncated: bool = False
    location_matched_total: int = 0
    location_unmatched_total: int = 0


class PocketSignalCursorResetIn(BaseModel):
    reason: str = ""


class PocketSignalCursorResetOut(BaseModel):
    generated_at: str
    reset_at: str = ""
    reason: str = ""
    cursor_updated_at: str = ""
    cursor_recording_id: str = ""
    cursor_cleared: bool = True


class PocketRecordingDetailOut(BaseModel):
    recording_id: str
    title: str = ""
    state: str = ""
    duration: float | None = None
    language: str = ""
    recording_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    tags: list[str] = Field(default_factory=list)
    transcript_text: str = ""
    transcript_segment_count: int = 0
    transcript_metadata: dict[str, object] = Field(default_factory=dict)
    summary_markdown: str = ""
    summary_id: str = ""
    audio_download_url: str = ""
    audio_expires_at: str = ""
    audio_expires_in: int | None = None
    transcript_quality_status: str = ""
    transcript_quality_score: float = 0.0
    transcript_quality_reasons: list[str] = Field(default_factory=list)
    retranscription_attempted: bool = False
    retranscription_status: str = ""
    preference_evidence_recorded: bool = False
    preference_evidence_applied_total: int = 0
    archive_path: str = ""
    archive_sha256: str = ""
    location_match_status: str = ""
    location_match_reason: str = ""
    location_name: str = ""
    location_address: str = ""
    location_latitude: float | None = None
    location_longitude: float | None = None
    location_start_at: str = ""
    location_end_at: str = ""
    location_source: str = ""
    location_confidence: float = 0.0


class PocketRecordingSearchItemOut(BaseModel):
    recording_id: str
    title: str = ""
    recording_at: str = ""
    archive_status: str = ""
    archive_path: str = ""
    archive_sha256: str = ""
    summary_markdown: str = ""
    transcript_excerpt: str = ""
    topic_keywords_csv: str = ""
    location_match_status: str = ""
    location_match_reason: str = ""
    location_name: str = ""
    location_address: str = ""
    location_latitude: float | None = None
    location_longitude: float | None = None
    location_start_at: str = ""
    location_end_at: str = ""
    location_source: str = ""
    location_confidence: float = 0.0
    match_score: float = 0.0


class PocketRecordingSearchOut(BaseModel):
    generated_at: str
    query: str = ""
    before: str = ""
    after: str = ""
    total: int = 0
    items: list[PocketRecordingSearchItemOut] = Field(default_factory=list)


class PocketRecordingTelegramDeliveryOut(BaseModel):
    recording_id: str
    title: str = ""
    telegram_delivery_status: str = ""
    telegram_delivery_error: str = ""
    telegram_message_ids: list[str] = Field(default_factory=list)
    telegram_chat_ref: str = ""
    audio_download_url: str = ""
    audio_expires_at: str = ""
    audio_ref: str = ""
    audio_enhancement: dict[str, object] = Field(default_factory=dict)


class PocketRecordingAudioEnhanceOut(BaseModel):
    recording_id: str
    title: str = ""
    enhancement_status: str = ""
    original_audio_path: str = ""
    original_audio_sha256: str = ""
    enhanced_audio_path: str = ""
    enhanced_audio_sha256: str = ""
    enhanced_metadata_path: str = ""
    filters_applied: list[str] = Field(default_factory=list)
    voice_profile_status: str = ""
    voice_profile_reason: str = ""


class PocketRecordingQueryTelegramDeliveryOut(PocketRecordingTelegramDeliveryOut):
    query: str = ""
    before: str = ""
    after: str = ""
    matched_total: int = 0
    location_name: str = ""
    location_address: str = ""
    location_match_status: str = ""
    location_confidence: float = 0.0


class OneDriveDocumentQueryTelegramDeliveryOut(BaseModel):
    query: str = ""
    matched_total: int = 0
    filename: str = ""
    document_path: str = ""
    document_download_url: str = ""
    answerly_data_item_id: str = ""
    telegram_delivery_status: str = ""
    telegram_delivery_error: str = ""
    telegram_message_ids: list[str] = Field(default_factory=list)
    telegram_chat_ref: str = ""


class GoogleSignalSyncOut(BaseModel):
    generated_at: str
    account_email: str = ""
    account_emails: list[str] = Field(default_factory=list)
    granted_scopes: list[str] = Field(default_factory=list)
    items: list[OfficeSignalResultOut] = Field(default_factory=list)
    total: int = 0
    synced_total: int = 0
    deduplicated_total: int = 0
    suppressed_total: int = 0


class PropertyScoutSourceOut(BaseModel):
    source_url: str = ""
    source_label: str = ""
    preference_person_id: str = "self"
    listing_total: int = 0
    duplicate_listing_total: int = 0
    review_created_total: int = 0
    review_existing_total: int = 0
    notified_total: int = 0
    email_notified_total: int = 0
    tour_created_total: int = 0
    tour_existing_total: int = 0
    high_fit_total: int = 0
    watch_notified_total: int = 0
    notification_score_suppressed_total: int = 0
    top_fit_score: float = 0.0
    top_candidates: list[dict[str, object]] = Field(default_factory=list)
    error: str = ""


class PropertyScoutSyncOut(BaseModel):
    generated_at: str
    status: str = "noop"
    sources_total: int = 0
    listing_total: int = 0
    duplicate_listing_total: int = 0
    review_created_total: int = 0
    review_existing_total: int = 0
    notified_total: int = 0
    email_notified_total: int = 0
    tour_created_total: int = 0
    tour_existing_total: int = 0
    high_fit_total: int = 0
    watch_notified_total: int = 0
    notification_score_suppressed_total: int = 0
    scout_outbound_min_score: float = 60.0
    failed_total: int = 0
    sources: list[PropertyScoutSourceOut] = Field(default_factory=list)


PROPERTY_SEARCH_RUN_MAX_SELECTED_PLATFORMS = 64


class PropertySearchRunStartIn(StrictMutationIn):
    selected_platforms: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list,
        # Agent plans may select every provider available in one market.
        # Austria currently exceeds the historical 24-provider transport cap,
        # so keep the request bounded above the catalog size while plan and
        # country eligibility remain enforced at the service boundary.
        max_length=PROPERTY_SEARCH_RUN_MAX_SELECTED_PLATFORMS,
    )
    property_preferences: dict[str, object] = Field(default_factory=dict, max_length=256)
    force_refresh: bool = False
    max_results_per_source: int | None = Field(default=None, ge=1, le=200)
    dispatch_only: bool = False

    @field_validator("selected_platforms")
    @classmethod
    def _normalize_platforms(cls, value: list[str]) -> list[str]:
        normalized = [str(item or "").strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("selected_platform_invalid")
        return normalized

    @field_validator("property_preferences")
    @classmethod
    def _bounded_property_preferences(cls, value: dict[str, object]) -> dict[str, object]:
        _validate_bounded_json_value(
            value,
            max_depth=5,
            max_items=256,
            max_string_length=2_000,
        )
        numeric_limits = {
            "min_price_eur": (0.0, 1_000_000_000.0),
            "max_price_eur": (0.0, 1_000_000_000.0),
            "min_rooms": (0.0, 100.0),
            "min_area_m2": (0.0, 100_000.0),
            "available_within_years": (0.0, 100.0),
            "max_commute_minutes_transit": (0.0, 1_440.0),
            "max_commute_minutes_drive": (0.0, 1_440.0),
            "max_commute_minutes_bike": (0.0, 1_440.0),
            "max_commute_minutes_walk": (0.0, 1_440.0),
        }
        for key, (minimum, maximum) in numeric_limits.items():
            raw = value.get(key)
            if raw is None or raw == "":
                continue
            if isinstance(raw, bool):
                raise ValueError(f"{key}_invalid")
            try:
                parsed = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}_invalid") from exc
            if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
                raise ValueError(f"{key}_out_of_range")

        validated = dict(value)
        raw_radius_unit = value.get("adjacent_area_radius_unit")
        if raw_radius_unit is None or raw_radius_unit == "":
            radius_unit = "m"
        else:
            if not isinstance(raw_radius_unit, str):
                raise ValueError("adjacent_area_radius_unit_invalid")
            radius_unit = raw_radius_unit.strip().lower()
            if radius_unit not in {"m", "km"}:
                raise ValueError("adjacent_area_radius_unit_invalid")
            validated["adjacent_area_radius_unit"] = radius_unit

        maximum_radius_m = 1_000_000.0
        for key, multiplier in (
            ("adjacent_area_radius_m", 1.0),
            ("adjacent_area_radius_value", 1_000.0 if radius_unit == "km" else 1.0),
        ):
            raw = value.get(key)
            if raw is None or raw == "":
                continue
            if isinstance(raw, bool):
                raise ValueError(f"{key}_invalid")
            try:
                parsed = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}_invalid") from exc
            radius_m = parsed * multiplier
            if (
                not math.isfinite(parsed)
                or not math.isfinite(radius_m)
                or radius_m < 0.0
                or radius_m > maximum_radius_m
            ):
                raise ValueError(f"{key}_out_of_range")
        return validated


class PropertySearchResearchTaskUpdateIn(BaseModel):
    action: str = Field(pattern="^(dismiss|fill|block|reopen)$")
    value: str = Field(default="", max_length=240)
    note: str = Field(default="", max_length=500)


class PropertyFactEnrichmentStartIn(StrictMutationIn):
    retry_failed: StrictBool = False


class PropertyFactProvenanceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="", max_length=80)
    method: Literal["", "straight_line_osm", "straight_line_google_maps"] = ""
    observed_at: str = Field(default="", max_length=64)
    expires_at: str = Field(default="", max_length=64)
    freshness: Literal["", "fresh", "stale", "unknown"] = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    source_key: str = Field(default="", max_length=80)
    observed_key: str = Field(default="", max_length=80)
    listing_url: str = Field(default="", max_length=2048)
    source_fingerprint: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    coordinate_basis: Literal["", "candidate_listing_coordinates", "listing_preview_coordinates"] = ""
    coordinate_observed_at: str = Field(default="", max_length=64)
    coordinate_precision: str = Field(default="unknown", min_length=1, max_length=40)
    coordinate_source: str = Field(default="", max_length=120)
    coordinate_exact: StrictBool = False
    listing_latitude: float | None = Field(default=None, ge=-90.0, le=90.0, allow_inf_nan=False)
    listing_longitude: float | None = Field(default=None, ge=-180.0, le=180.0, allow_inf_nan=False)
    coordinate_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    query_endpoint_url: str = Field(default="", max_length=500)
    query_url: str = Field(default="", max_length=2048)
    query_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    query_schema: str = Field(default="", max_length=80)
    receipt_url: str = Field(default="", max_length=2048)
    provider_object_id: str = Field(default="", max_length=120)
    provider_object_type: str = Field(default="", max_length=80)
    provider_object_version: str = Field(default="", max_length=80)
    provider_object_timestamp: str = Field(default="", max_length=64)
    provider_object_changeset: str = Field(default="", max_length=80)
    provider_observed_at: str = Field(default="", max_length=64)
    provider_expires_at: str = Field(default="", max_length=64)
    attestation_version: str = Field(default="", max_length=96)
    provider_attestation: str = Field(
        default="",
        pattern=r"^(|hmac-sha256:[0-9a-f]{64})$",
    )
    poi_latitude: float | None = Field(default=None, ge=-90.0, le=90.0, allow_inf_nan=False)
    poi_longitude: float | None = Field(default=None, ge=-180.0, le=180.0, allow_inf_nan=False)
    poi_classification_tags: dict[str, str] = Field(default_factory=dict, max_length=8)

    @field_validator(
        "observed_at",
        "expires_at",
        "coordinate_observed_at",
        "provider_object_timestamp",
        "provider_observed_at",
        "provider_expires_at",
    )
    @classmethod
    def _validate_optional_timestamp(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return ""
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid_fact_timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("fact_timestamp_timezone_required")
        return normalized

    @field_validator("poi_classification_tags")
    @classmethod
    def _validate_classification_tags(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not str(key).strip()
            or len(str(key)) > 80
            or not str(tag_value).strip()
            or len(str(tag_value)) > 160
            for key, tag_value in value.items()
        ):
            raise ValueError("invalid_fact_classification_tags")
        return value


class PropertyFactErrorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(default="", pattern=r"^[a-z0-9_]{0,96}$")
    message: str = Field(default="", max_length=240)
    retry_after_seconds: int = Field(default=0, ge=0, le=86_400)


class PropertyFactFieldOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="", max_length=80)
    provider_label: str = Field(default="", max_length=120)
    strict_evidence_provider: StrictBool = False
    state: Literal["unknown", "stale", "queued", "running", "resolved", "retryable_error", "unavailable"]
    priority: Literal["required", "lazy"]
    affects_score: StrictBool
    value: int | float | str | StrictBool | None = None
    display_value: str = Field(default="", max_length=120)
    provenance: PropertyFactProvenanceOut = Field(default_factory=PropertyFactProvenanceOut)
    error: PropertyFactErrorOut = Field(default_factory=PropertyFactErrorOut)

    @field_validator("value")
    @classmethod
    def _validate_fact_value(cls, value: object) -> object:
        if isinstance(value, float) and (not math.isfinite(value) or abs(value) > 5_000_000):
            raise ValueError("fact_numeric_value_out_of_range")
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 5_000_000:
            raise ValueError("fact_numeric_value_out_of_range")
        if isinstance(value, str) and len(value) > 240:
            raise ValueError("fact_string_value_too_long")
        return value


class PropertyFactScoreOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["evaluating", "provisional", "final"]
    previous: float | None = Field(default=None, ge=0.0, le=100.0, allow_inf_nan=False)
    current: float | None = Field(default=None, ge=0.0, le=100.0, allow_inf_nan=False)
    delta: float | None = Field(default=None, ge=-100.0, le=100.0, allow_inf_nan=False)
    changed_reasons: list[str] = Field(default_factory=list, max_length=8)
    ranking_eligible: StrictBool
    algorithm_version: Literal["propertyquarry.fact-score-state.v1"]
    facts_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preference_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("changed_reasons")
    @classmethod
    def _validate_changed_reasons(cls, value: list[str]) -> list[str]:
        if any(not str(reason).strip() or len(str(reason)) > 240 for reason in value):
            raise ValueError("invalid_score_changed_reason")
        return value


class PropertyFactProviderReceiptOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    provider: str = Field(default="", max_length=80)
    provider_label: str = Field(default="", max_length=120)
    status: Literal["verified", "pending", "unavailable"]
    evidence_source_key: str = Field(default="", max_length=80)
    source_fingerprint: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    coordinate_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    query_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    receipt_url: str = Field(default="", max_length=2048)
    provider_object_id: str = Field(default="", max_length=120)
    provider_object_type: str = Field(default="", max_length=80)
    provider_object_version: str = Field(default="", max_length=80)
    reason_code: str = Field(default="", pattern=r"^[a-z0-9_]{0,96}$")


class PropertyOneminJudgmentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation: Literal["", "shortlist", "consider", "hold", "reject"] = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    summary: str = Field(default="", max_length=600)
    strengths: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=6)
    evidence_keys: list[str] = Field(default_factory=list, max_length=12)
    missing_fact_keys: list[str] = Field(default_factory=list, max_length=12)


class PropertyOneminProviderReceiptOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="", max_length=80)
    query_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    receipt_url: str = Field(default="", max_length=2048)
    provider_object_id: str = Field(default="", max_length=120)
    coordinate_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")


class PropertyOneminBrowserReceiptOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str = Field(default="", max_length=120)
    account_ref: str = Field(default="", max_length=160)
    work_type: Literal["research"] = "research"
    task_summary: str = Field(default="", max_length=300)
    requested_actions: list[str] = Field(default_factory=list, max_length=6)
    completed_actions: list[str] = Field(default_factory=list, max_length=6)
    context_used: list[str] = Field(default_factory=list, max_length=6)
    quality_gate: str = Field(default="", max_length=300)
    staged_items: list[str] = Field(default_factory=list, max_length=6)
    final_surface_url: str = Field(default="", max_length=2048)
    total_visible: str = Field(default="", max_length=80)
    notification_policy: Literal["action_required_only"] = "action_required_only"
    stop_condition: str = Field(default="", max_length=80)
    irreversible_actions_attempted: list[str] = Field(default_factory=list, max_length=0)
    blockers: list[str] = Field(default_factory=list, max_length=6)
    evidence: dict[str, str] = Field(default_factory=dict, max_length=2)


class PropertyOneminOodaActionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(default="", max_length=40)
    fact_key: str = Field(default="", max_length=80, pattern=r"^[a-z0-9_]*$")
    label: str = Field(default="", max_length=120)
    provider: Literal["google_maps_browseract"] = "google_maps_browseract"
    work_type: Literal["research"] = "research"
    reason: str = Field(default="", max_length=240)
    travel_mode: Literal["walking", "driving", "bicycling", "transit"] = "walking"
    priority: int = Field(default=1, ge=1, le=9)
    status: Literal["planned", "verified", "unavailable", "blocked", "skipped"] = "planned"
    observed_distance_m: int | None = Field(default=None, ge=1, le=5_000_000)
    place_name: str = Field(default="", max_length=160)
    blockers: list[str] = Field(default_factory=list, max_length=6)
    provider_receipt: PropertyOneminProviderReceiptOut = Field(
        default_factory=PropertyOneminProviderReceiptOut
    )
    browser_receipt: PropertyOneminBrowserReceiptOut = Field(
        default_factory=PropertyOneminBrowserReceiptOut
    )


class PropertyOneminOodaOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["propertyquarry.onemin-ooda.v1"]
    phase: Literal["observe", "orient", "decide", "act"] = "observe"
    actions: list[PropertyOneminOodaActionOut] = Field(default_factory=list, max_length=2)


class PropertyOneminManagerReceiptOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: Literal["provider.onemin.code_generate"] = "provider.onemin.code_generate"
    action_kind: Literal["property.evaluate"] = "property.evaluate"
    provider: Literal["1minAI"] = "1minAI"
    provider_backend: str = Field(default="", max_length=80)
    provider_account_name: str = Field(default="", max_length=120)
    provider_key_slot: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=120)
    tokens_in: int = Field(default=0, ge=0, le=10_000_000)
    tokens_out: int = Field(default=0, ge=0, le=10_000_000)
    manager_routed: StrictBool = False
    input_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    evaluated_at: str = Field(default="", max_length=64)


class PropertyOneminErrorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(default="", pattern=r"^[a-z0-9_]{0,96}$")


class PropertyOneminEvaluationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["propertyquarry.onemin-evaluation.v1"]
    status: Literal["disabled", "unavailable", "succeeded"]
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manager_routed: StrictBool = False
    cache_hit: StrictBool = False
    evaluated_at: str = Field(default="", max_length=64)
    judgment: PropertyOneminJudgmentOut = Field(default_factory=PropertyOneminJudgmentOut)
    ooda: PropertyOneminOodaOut
    receipt: PropertyOneminManagerReceiptOut = Field(
        default_factory=PropertyOneminManagerReceiptOut
    )
    error: PropertyOneminErrorOut = Field(default_factory=PropertyOneminErrorOut)


class PropertyFactEnrichmentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["propertyquarry.fact-enrichment.v1"]
    job_id: str = Field(min_length=5, max_length=40, pattern=r"^pfe_[0-9a-f]{24}$")
    bundle_kind: Literal["optional-geo-v1", "required-geo-v1"]
    run_id: str = Field(min_length=1, max_length=160)
    candidate_ref: str = Field(min_length=1, max_length=160)
    status: Literal["idle", "queued", "running", "succeeded", "retryable_error", "terminal_error"]
    status_url: str = Field(min_length=1, max_length=500)
    attempt: int = Field(ge=0, le=3)
    poll_after_ms: int = Field(ge=500, le=10_000)
    updated_at: str = Field(default="", max_length=64)
    retryable: StrictBool
    fields: list[PropertyFactFieldOut] = Field(default_factory=list, max_length=32)
    score: PropertyFactScoreOut
    provider_receipts: list[PropertyFactProviderReceiptOut] = Field(
        default_factory=list,
        max_length=32,
    )
    onemin_evaluation: PropertyOneminEvaluationOut

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return ""
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid_fact_job_timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("fact_job_timestamp_timezone_required")
        return normalized


class PropertySearchRunStatusOut(BaseModel):
    generated_at: str
    created_at: str = ""
    updated_at: str = ""
    run_id: str = ""
    principal_id: str = ""
    status: str = "queued"
    status_url: str = ""
    selected_platforms: list[str] = Field(default_factory=list)
    progress: int = 0
    current_step: str = ""
    message: str = ""
    stages_total: int = 0
    steps_completed: int = 0
    provider_display_total: int = 0
    source_variant_display_total: int = 0
    selected_platform_count: int = 0
    summary: dict[str, object] = Field(default_factory=dict)
    events: list[dict[str, object]] = Field(default_factory=list)
    research_tasks: list[dict[str, object]] = Field(default_factory=list)
    research_task_total: int = 0
    open_research_task_total: int = 0
    filled_research_task_total: int = 0
    dismissed_research_task_total: int = 0
    bootstrap_required: bool = False
    bootstrap_country_code: str = ""
    bootstrap_country_label: str = ""
    bootstrap_eta_hours: int = 0
    bootstrap_handoff_ref: str = ""


class PropertySearchRunStartOut(PropertySearchRunStatusOut):
    pass


class PropertyBillingCheckoutCreateIn(BaseModel):
    plan_key: str = Field(min_length=1, max_length=40)


class PropertyBillingCheckoutOut(BaseModel):
    generated_at: str
    plan_key: str
    order_id: str
    approve_url: str
    status: str = ""
    amount_eur: str = ""


class PropertyBillingCaptureIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=80)
    plan_key: str = Field(min_length=1, max_length=40)


class PropertyBillingCaptureOut(BaseModel):
    generated_at: str
    order_id: str
    plan_key: str
    capture_id: str = ""
    payment_status: str = ""
    payer_email: str = ""
    amount_eur: str = ""
    active_until: str = ""
    current_plan_key: str = "free"


class GooglePhotosPickerSessionIn(StrictMutationIn):
    account_email: str = Field(default="", max_length=320)
    binding_id: str = Field(default="", max_length=240)
    max_item_count: int = Field(default=50, ge=1, le=2000)
    autoclose: bool = True


class GooglePhotosPickerSessionOut(BaseModel):
    generated_at: str
    status: str = "ready_for_selection"
    account_email: str = ""
    binding_id: str = ""
    granted_scopes: list[str] = Field(default_factory=list)
    session_id: str = ""
    picker_uri: str = ""
    poll_interval: str = ""
    timeout_in: str = ""
    media_items_set: bool = False


class GooglePhotosSignalSyncIn(StrictMutationIn):
    session_id: str = Field(min_length=1, max_length=500)
    account_email: str = Field(default="", max_length=320)
    binding_id: str = Field(default="", max_length=240)
    max_items: int = Field(default=50, ge=1, le=500)
    delete_session: bool = False


class GooglePhotosSignalSyncOut(BaseModel):
    generated_at: str
    account_email: str = ""
    account_emails: list[str] = Field(default_factory=list)
    binding_id: str = ""
    session_id: str = ""
    granted_scopes: list[str] = Field(default_factory=list)
    media_items_set: bool = False
    items: list[OfficeSignalResultOut] = Field(default_factory=list)
    total: int = 0
    selected_total: int = 0
    synced_total: int = 0
    deduplicated_total: int = 0
    suppressed_total: int = 0
    analyzed_total: int = 0
    suggestion_total: int = 0
    top_suggestions: list[str] = Field(default_factory=list)


class GoogleSignalSyncStatusOut(BaseModel):
    generated_at: str
    connected: bool = False
    account_email: str = ""
    account_emails: list[str] = Field(default_factory=list)
    token_status: str = "missing"
    last_refresh_at: str = ""
    reauth_required_reason: str = ""
    sync_completed: int = 0
    office_signal_ingested: int = 0
    last_completed_at: str = ""
    last_synced_total: int = 0
    last_deduplicated_total: int = 0
    last_suppressed_total: int = 0
    last_gmail_total: int = 0
    last_calendar_total: int = 0
    age_seconds: int | None = None
    freshness_state: str = "watch"
    account_sync_accounts: list[dict[str, object]] = Field(default_factory=list)
    pending_commitment_candidates: int = 0
    covered_signal_candidates: int = 0
    last_account_change_at: str = ""
    last_account_change_state: str = ""
    last_account_change_binding_id: str = ""
    last_account_change_email: str = ""
    account_change_accounts: list[dict[str, object]] = Field(default_factory=list)


class WebhookRegisterIn(BaseModel):
    label: str = Field(min_length=1)
    target_url: str = Field(min_length=1)
    event_types: list[str] = Field(default_factory=list)
    status: str = "active"


class WebhookTestResultOut(BaseModel):
    webhook: WebhookOut
    delivery: WebhookDeliveryOut


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_out(values: tuple[EvidenceRef, ...]) -> list[EvidenceRefOut]:
    return [EvidenceRefOut(**value.__dict__) for value in values]


def brief_out(value: BriefItem) -> BriefItemOut:
    return BriefItemOut(
        id=value.id,
        workspace_id=value.workspace_id,
        kind=value.kind,
        title=value.title,
        summary=value.summary,
        score=value.score,
        why_now=value.why_now,
        evidence_refs=evidence_out(value.evidence_refs),
        related_people=list(value.related_people),
        related_commitment_ids=list(value.related_commitment_ids),
        recommended_action=value.recommended_action,
        status=value.status,
        confidence=value.confidence,
        object_ref=value.object_ref,
        profile_followup_refs=list(value.profile_followup_refs),
        evidence_count=value.evidence_count,
    )


def queue_out(value: DecisionQueueItem) -> DecisionQueueItemOut:
    return DecisionQueueItemOut(
        id=value.id,
        queue_kind=value.queue_kind,
        title=value.title,
        summary=value.summary,
        priority=value.priority,
        rank_score=value.rank_score,
        deadline=value.deadline,
        owner_role=value.owner_role,
        requires_principal=value.requires_principal,
        evidence_refs=evidence_out(value.evidence_refs),
        profile_followup_refs=list(value.profile_followup_refs),
        resolution_state=value.resolution_state,
    )


def decision_out(value: DecisionItem) -> DecisionItemOut:
    return DecisionItemOut(
        id=value.id,
        title=value.title,
        summary=value.summary,
        priority=value.priority,
        owner_role=value.owner_role,
        due_at=value.due_at,
        status=value.status,
        decision_type=value.decision_type,
        recommendation=value.recommendation,
        next_action=value.next_action,
        rationale=value.rationale,
        options=list(value.options),
        evidence_refs=evidence_out(value.evidence_refs),
        related_commitment_ids=list(value.related_commitment_ids),
        linked_thread_ids=list(value.linked_thread_ids),
        related_people=list(value.related_people),
        impact_summary=value.impact_summary,
        sla_status=value.sla_status,
        resolution_reason=value.resolution_reason,
    )


def deadline_out(value: DeadlineItem) -> DeadlineOut:
    return DeadlineOut(
        id=value.id,
        title=value.title,
        summary=value.summary,
        priority=value.priority,
        start_at=value.start_at,
        end_at=value.end_at,
        status=value.status,
    )


def commitment_out(value: CommitmentItem) -> CommitmentOut:
    return CommitmentOut(
        id=value.id,
        source_type=value.source_type,
        source_ref=value.source_ref,
        statement=value.statement,
        owner=value.owner,
        counterparty=value.counterparty,
        due_at=value.due_at,
        status=value.status,
        last_activity_at=value.last_activity_at,
        risk_level=value.risk_level,
        proof_refs=evidence_out(value.proof_refs),
        confidence=value.confidence,
        channel_hint=value.channel_hint,
        resolution_code=value.resolution_code,
        resolution_reason=value.resolution_reason,
        duplicate_of_ref=value.duplicate_of_ref,
        merged_into_ref=value.merged_into_ref,
        merged_from_refs=list(value.merged_from_refs),
    )


def commitment_candidate_out(value: CommitmentCandidate) -> CommitmentCandidateOut:
    return CommitmentCandidateOut(
        candidate_id=value.candidate_id,
        title=value.title,
        details=value.details,
        source_text=value.source_text,
        confidence=value.confidence,
        suggested_due_at=value.suggested_due_at,
        counterparty=value.counterparty,
        channel_hint=value.channel_hint,
        source_ref=value.source_ref,
        signal_type=value.signal_type,
        status=value.status,
        kind=value.kind,
        stakeholder_id=value.stakeholder_id,
        duplicate_of_ref=value.duplicate_of_ref,
        merge_strategy=value.merge_strategy,
    )


def draft_out(value: DraftCandidate) -> DraftCandidateOut:
    return DraftCandidateOut(
        id=value.id,
        thread_ref=value.thread_ref,
        recipient_summary=value.recipient_summary,
        intent=value.intent,
        draft_text=value.draft_text,
        tone=value.tone,
        requires_approval=value.requires_approval,
        approval_status=value.approval_status,
        provenance_refs=evidence_out(value.provenance_refs),
        send_channel=value.send_channel,
    )


def person_out(value: PersonProfile) -> PersonProfileOut:
    return PersonProfileOut(
        id=value.id,
        display_name=value.display_name,
        role_or_company=value.role_or_company,
        importance_score=value.importance_score,
        relationship_temperature=value.relationship_temperature,
        open_loops_count=value.open_loops_count,
        latest_touchpoint_at=value.latest_touchpoint_at,
        preferred_tone=value.preferred_tone,
        themes=list(value.themes),
        risks=list(value.risks),
    )


def handoff_out(value: HandoffNote) -> HandoffNoteOut:
    return HandoffNoteOut(
        id=value.id,
        queue_item_ref=value.queue_item_ref,
        summary=value.summary,
        owner=value.owner,
        due_time=value.due_time,
        escalation_status=value.escalation_status,
        status=value.status,
        task_type=value.task_type,
        resolution=value.resolution,
        draft_ref=value.draft_ref,
        recipient_email=value.recipient_email,
        subject=value.subject,
        delivery_reason=value.delivery_reason,
        property_url=value.property_url,
        listing_id=value.listing_id,
        variant_key=value.variant_key,
        blocked_reason=value.blocked_reason,
        tour_url=value.tour_url,
        vendor_tour_url=value.vendor_tour_url,
        editor_url=value.editor_url,
        connector_binding_id=value.connector_binding_id,
        source_ref=value.source_ref,
        external_id=value.external_id,
        evidence_refs=evidence_out(value.evidence_refs),
    )


def handoff_assignment_history_out(event: ExecutionEvent) -> HandoffAssignmentHistoryOut:
    payload = dict(event.payload or {})
    return HandoffAssignmentHistoryOut(
        event_id=event.event_id,
        human_task_id=str(payload.get("human_task_id") or ""),
        event_name=event.name,
        assignment_state=str(payload.get("assignment_state") or ""),
        assigned_operator_id=str(payload.get("assigned_operator_id") or payload.get("operator_id") or ""),
        assignment_source=str(payload.get("assignment_source") or ""),
        assigned_at=str(payload.get("assigned_at") or "") or None,
        assigned_by_actor_id=str(payload.get("assigned_by_actor_id") or ""),
        resolution=str(payload.get("resolution") or ""),
        created_at=event.created_at,
    )


def thread_out(value: ThreadItem) -> ThreadItemOut:
    return ThreadItemOut(
        id=value.id,
        title=value.title,
        channel=value.channel,
        status=value.status,
        last_activity_at=value.last_activity_at,
        summary=value.summary,
        counterparties=list(value.counterparties),
        draft_ids=list(value.draft_ids),
        related_commitment_ids=list(value.related_commitment_ids),
        related_decision_ids=list(value.related_decision_ids),
        evidence_refs=evidence_out(value.evidence_refs),
    )


def evidence_item_out(value: EvidenceItem) -> EvidenceItemOut:
    return EvidenceItemOut(
        id=value.id,
        label=value.label,
        source_type=value.source_type,
        summary=value.summary,
        href=value.href,
        related_object_refs=list(value.related_object_refs),
    )


def rule_out(value: RuleItem) -> RuleItemOut:
    return RuleItemOut(
        id=value.id,
        label=value.label,
        scope=value.scope,
        status=value.status,
        summary=value.summary,
        current_value=value.current_value,
        impact=value.impact,
        requires_approval=value.requires_approval,
        simulated_effect=value.simulated_effect,
    )


def history_out(value: HistoryEntry) -> HistoryEntryOut:
    return HistoryEntryOut(
        event_type=value.event_type,
        created_at=value.created_at,
        source_id=value.source_id,
        actor=value.actor,
        detail=value.detail,
    )


def person_detail_out(value: PersonDetail) -> PersonDetailOut:
    return PersonDetailOut(
        profile=person_out(value.profile),
        commitments=[commitment_out(item) for item in value.commitments],
        drafts=[draft_out(item) for item in value.drafts],
        threads=[thread_out(item) for item in value.threads],
        queue_items=[queue_out(item) for item in value.queue_items],
        handoffs=[handoff_out(item) for item in value.handoffs],
        evidence_refs=evidence_out(value.evidence_refs),
        history=[history_out(item) for item in value.history],
    )
