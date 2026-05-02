"""
Tool I/O schemas for the mediation assistant.

Every tool the agentic loop can call has an input model, an output model, and
explicit error/edge variants. The LLM sees the input schema; the orchestrator
validates against the output schema before returning. Keep all enums and types
here so there is one source of truth.

Pydantic v2.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared enums and primitives
# ---------------------------------------------------------------------------


class Charge(str, Enum):
    routine = "routine"
    notable = "notable"
    charged = "charged"
    crisis = "crisis"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class MemoryStatus(str, Enum):
    active = "active"
    superseded = "superseded"
    invalidated = "invalidated"


class ThemeStatus(str, Enum):
    active = "active"
    dormant = "dormant"
    resolved = "resolved"
    resolved_by_time = "resolved_by_time"


class ThemeSentiment(str, Enum):
    improving = "improving"
    stable = "stable"
    worsening = "worsening"
    mixed = "mixed"


class ThemeHealth(str, Enum):
    healthy = "healthy"
    tender = "tender"
    strained = "strained"
    inflamed = "inflamed"


class WatchStatus(str, Enum):
    open = "open"
    addressed = "addressed"
    expired = "expired"
    cancelled = "cancelled"


class ObservationStatus(str, Enum):
    active = "active"
    contradicted = "contradicted"
    stale = "stale"


class OOBSeverity(str, Enum):
    soft = "soft"
    firm = "firm"
    hard = "hard"


class OOBStatus(str, Enum):
    active = "active"
    expired = "expired"
    lifted = "lifted"


class FeedbackSentiment(str, Enum):
    positive = "positive"
    negative = "negative"
    mixed = "mixed"


class CrossThreadSharingDefault(str, Enum):
    opt_in = "opt_in"
    opt_out = "opt_out"


class BridgeCandidateKind(str, Enum):
    context = "context"
    clarification = "clarification"
    contradiction = "contradiction"
    repair = "repair"
    vulnerability = "vulnerability"
    process = "process"


class BridgeCandidateStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    sent = "sent"
    declined = "declined"
    blocked = "blocked"
    addressed = "addressed"
    expired = "expired"


class BridgeCandidateSensitivity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


Significance = Annotated[int, Field(ge=1, le=5)]


class DateRange(BaseModel):
    start: datetime | None = None
    end: datetime | None = None


# ---------------------------------------------------------------------------
# Standard write-tool result envelope
#
# Write tools return a discriminated outcome so the LLM can react to dedup hits
# without us silently merging behind its back.
# ---------------------------------------------------------------------------


class WriteCreated(BaseModel):
    action: Literal["created"] = "created"
    id: UUID


class WriteUpdated(BaseModel):
    action: Literal["updated"] = "updated"
    id: UUID


# Note: dedup is the agent's job. It searches existing rows before writing and
# picks add vs update vs supersede explicitly. Write tools don't gate on
# similarity, so there's no WriteDuplicate / WriteReinforced variant.


# ---------------------------------------------------------------------------
# READ TOOLS
# ---------------------------------------------------------------------------


# --- search_messages ---


class SearchMessagesInput(BaseModel):
    partner_user_id: UUID | None = Field(
        default=None, description="If set, restrict to that partner's thread."
    )
    date_range: DateRange | None = None
    text_contains: str | None = Field(
        default=None,
        description="Plain substring match. Case-insensitive. Empty string = no filter.",
    )
    limit: int = Field(default=50, ge=1, le=500)


class MessageHit(BaseModel):
    id: UUID
    sender_id: UUID | None
    sent_at: datetime
    content: str
    charge: Charge
    direction: Literal["inbound", "outbound"]


class SearchMessagesOutput(BaseModel):
    hits: list[MessageHit]
    truncated: bool


# --- search_emojis ---


class SearchEmojisInput(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=120,
        description="Meaning, feeling, metaphor, or plain-language idea to search for, e.g. 'fragile peace', 'reluctant leaving', 'steady support'.",
    )
    limit: int = Field(default=12, ge=1, le=50)


class EmojiSearchHit(BaseModel):
    emoji: str
    name: str
    aliases: list[str] = []
    keywords: list[str] = []
    score: int = Field(ge=0)


class SearchEmojisOutput(BaseModel):
    query: str
    hits: list[EmojiSearchHit]
    used_full_dataset: bool


# --- recent_activity ---


class RecentActivityInput(BaseModel):
    days: int = Field(ge=1, le=90)


class ThreadDigest(BaseModel):
    user_id: UUID
    user_name: str
    message_count: int
    last_message_at: datetime | None
    summary: str  # LLM-generated digest, prepared by the tool


class RecentActivityOutput(BaseModel):
    threads: list[ThreadDigest]
    period: DateRange


# --- list_themes / get_theme ---


class ThemeSortBy(str, Enum):
    last_reinforced = "last_reinforced"
    last_active = "last_active"
    created = "created"


class ListThemesInput(BaseModel):
    active_only: bool = True
    sort_by: ThemeSortBy = ThemeSortBy.last_reinforced
    limit: int = Field(default=50, ge=1, le=200)


class ThemeSummary(BaseModel):
    id: UUID
    title: str
    status: ThemeStatus
    sentiment: ThemeSentiment
    health: ThemeHealth
    last_reinforced_at: datetime | None
    last_active_at: datetime | None


class ListThemesOutput(BaseModel):
    themes: list[ThemeSummary]


class GetThemeInput(BaseModel):
    theme_id: UUID


class ThemeDetail(ThemeSummary):
    description: str
    first_seen_at: datetime
    related_memory_ids: list[UUID]
    related_observation_ids: list[UUID]


class GetThemeOutput(BaseModel):
    theme: ThemeDetail | None  # None if not found


# --- get_memories ---


class GetMemoriesInput(BaseModel):
    about_user_id: UUID | None = Field(
        default=None,
        description="None means 'any', including couple-level. Pass an explicit sentinel if you want couple-only.",
    )
    couple_only: bool = False
    status: MemoryStatus = MemoryStatus.active
    theme_id: UUID | None = None
    limit: int = Field(default=100, ge=1, le=500)


class MemoryRow(BaseModel):
    id: UUID
    about_user_id: UUID | None
    content: str
    status: MemoryStatus
    related_theme_ids: list[UUID]
    created_at: datetime
    last_referenced_at: datetime | None


class GetMemoriesOutput(BaseModel):
    memories: list[MemoryRow]


# --- list_watch_items ---


class ListWatchItemsInput(BaseModel):
    owner_user_id: UUID | None = None
    status: WatchStatus | None = WatchStatus.open
    due_before: datetime | None = None


class WatchItemRow(BaseModel):
    id: UUID
    owner_user_id: UUID
    content: str
    due_at: datetime | None
    status: WatchStatus
    addressing_note: str | None
    created_at: datetime
    addressed_at: datetime | None
    related_theme_ids: list[UUID]


class ListWatchItemsOutput(BaseModel):
    items: list[WatchItemRow]


# --- get_observations ---


class GetObservationsInput(BaseModel):
    theme_id: UUID | None = None
    status: ObservationStatus = ObservationStatus.active
    about_user_id: UUID | None = None
    min_significance: Significance | None = None
    limit: int = Field(default=100, ge=1, le=500)


class ObservationRow(BaseModel):
    id: UUID
    content: str
    about_user_id: UUID | None
    confidence: Confidence
    significance: Significance | None  # None if scoring failed and not yet rescored
    status: ObservationStatus
    related_theme_ids: list[UUID]
    supporting_message_ids: list[UUID]
    created_at: datetime
    last_reinforced_at: datetime | None
    surfaced_count: int


class GetObservationsOutput(BaseModel):
    observations: list[ObservationRow]


# --- get_oob ---


class GetOOBInput(BaseModel):
    owner_id: UUID | None = None  # None = both partners
    include_lifted: bool = False


class OOBRow(BaseModel):
    id: UUID
    owner_id: UUID
    protected_summary: str = Field(description="Safe, non-sensitive display text; never the raw protected core.")
    shareable_context: str | None
    severity: OOBSeverity
    status: OOBStatus
    created_at: datetime
    review_at: datetime | None


class GetOOBOutput(BaseModel):
    entries: list[OOBRow]


# --- summarize_oob_topics ---


class SummarizeOOBTopicsInput(BaseModel):
    owner_id: UUID = Field(description="The partner whose active OOB topic categories should be summarized.")


class OOBTopicCluster(BaseModel):
    count: int = Field(ge=1)
    topic: str


class SummarizeOOBTopicsOutput(BaseModel):
    total_count: int
    clusters: list[OOBTopicCluster]
    narrative: str


# --- check_oob ---
#
# Runs as a separate Sonnet call against active OOB entries for protected owners.
# Defaults to recipient-only checks for compatibility; final outbound callers
# should include both dyad owner ids when protecting both users is appropriate.
# Belt-and-suspenders to the in-prompt OOB awareness in the main loop.


class OOBVerdict(str, Enum):
    ok = "ok"
    rewrite = "rewrite"
    block = "block"


class CheckOOBInput(BaseModel):
    content: str
    recipient_id: UUID = Field(
        description="The outbound recipient. Used as the default protected owner when protected_owner_ids is omitted."
    )
    protected_owner_ids: list[UUID] | None = Field(
        default=None,
        description="Optional owner ids whose active OOB entries must be protected. Omit for recipient-only compatibility.",
    )
    sender_intent: str | None = Field(
        default=None,
        description="Short summary of why the bot is sending this; helps the checker judge intent.",
    )


class CheckOOBOutput(BaseModel):
    verdict: OOBVerdict
    reason: str
    triggering_oob_ids: list[UUID]
    suggested_rewrite: str | None  # only when verdict == rewrite
    checker_failed: bool = Field(
        default=False,
        description="True when the checker LLM call errored or timed out. Caller fails closed for firm/hard OOB, open for soft only.",
    )


# --- get_self_model ---


class GetSelfModelInput(BaseModel):
    user_id: UUID


class SelfModel(BaseModel):
    user_id: UUID
    name: str
    style_notes: str
    active_themes: list[ThemeSummary]
    memories: list[MemoryRow]
    high_significance_observations: list[ObservationRow]
    open_watch_items: list[WatchItemRow]


class GetSelfModelOutput(BaseModel):
    model: SelfModel


# --- get_bot_actions ---


class BotActionTargetType(str, Enum):
    message = "message"
    memory = "memory"
    observation = "observation"
    theme = "theme"
    watch_item = "watch_item"
    oob = "oob"
    schedule = "schedule"
    escalation = "escalation"


class GetBotActionsInput(BaseModel):
    date_range: DateRange | None = None
    target_type: BotActionTargetType | None = None
    user_in_context: UUID | None = None
    limit: int = Field(default=50, ge=1, le=500)


class BotAction(BaseModel):
    turn_id: UUID
    started_at: datetime
    user_in_context: UUID | None
    triggered_by_message_id: UUID | None
    final_output_message_id: UUID | None
    triggering_content: str | None = None
    final_outbound_content: str | None = None
    reasoning: str
    tool_calls: list[dict]  # raw tool_calls rows; the LLM can read them


class GetBotActionsOutput(BaseModel):
    actions: list[BotAction]


# ---------------------------------------------------------------------------
# WRITE TOOLS
# ---------------------------------------------------------------------------


# --- update_user_style_notes ---


class UpdateUserStyleNotesInput(BaseModel):
    user_id: UUID
    notes: str = Field(
        description="Full replacement of the living style-notes blob. Don't truncate intentionally."
    )


class UpdateUserStyleNotesOutput(BaseModel):
    user_id: UUID
    updated_at: datetime


# --- update_cross_thread_sharing_default ---


class UpdateCrossThreadSharingDefaultInput(BaseModel):
    user_id: UUID
    default: CrossThreadSharingDefault = Field(
        description="Whether this user's thread is shareable across the relationship bridge by default."
    )
    reason: str = Field(description="The user's stated preference or short rationale. Logged for audit.")


class UpdateCrossThreadSharingDefaultOutput(BaseModel):
    user_id: UUID
    default: CrossThreadSharingDefault
    updated_at: datetime


# --- bridge candidates ---


class BridgeCandidate(BaseModel):
    id: UUID
    source_user_id: UUID
    target_user_id: UUID
    kind: BridgeCandidateKind
    status: BridgeCandidateStatus
    sensitivity: BridgeCandidateSensitivity
    source_message_ids: list[UUID]
    related_memory_ids: list[UUID] = Field(default_factory=list)
    related_observation_ids: list[UUID] = Field(default_factory=list)
    shareable_summary: str
    internal_note: str | None = None
    sent_message_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None


class CreateBridgeCandidateInput(BaseModel):
    source_user_id: UUID
    target_user_id: UUID
    kind: BridgeCandidateKind
    sensitivity: BridgeCandidateSensitivity = BridgeCandidateSensitivity.medium
    source_message_ids: list[UUID] = Field(min_length=1)
    related_memory_ids: list[UUID] = Field(default_factory=list)
    related_observation_ids: list[UUID] = Field(default_factory=list)
    internal_note: str | None = None
    shareable_summary: str = Field(min_length=1)
    status: BridgeCandidateStatus | None = Field(
        default=None,
        description="Optional explicit lifecycle status. Runtime defaults are applied by the tool implementation.",
    )


class CreateBridgeCandidateOutput(BaseModel):
    candidate: BridgeCandidate


class ListBridgeCandidatesInput(BaseModel):
    source_user_id: UUID | None = None
    target_user_id: UUID | None = None
    status: BridgeCandidateStatus | None = None
    limit: int = Field(default=10, ge=1, le=50)


class ListBridgeCandidatesOutput(BaseModel):
    candidates: list[BridgeCandidate]
    truncated: bool = False


class UpdateBridgeCandidateInput(BaseModel):
    candidate_id: UUID
    kind: BridgeCandidateKind | None = None
    status: BridgeCandidateStatus | None = None
    sensitivity: BridgeCandidateSensitivity | None = None
    source_message_ids: list[UUID] | None = Field(default=None, min_length=1)
    related_memory_ids: list[UUID] | None = None
    related_observation_ids: list[UUID] | None = None
    internal_note: str | None = None
    shareable_summary: str | None = Field(default=None, min_length=1)


class UpdateBridgeCandidateOutput(BaseModel):
    candidate: BridgeCandidate


class SendBridgeCandidateInput(BaseModel):
    candidate_id: UUID
    reason: str | None = Field(
        default=None,
        description="Short reason for sending this ready bridge candidate now.",
    )


class SendBridgeCandidateOutput(BaseModel):
    candidate: BridgeCandidate


# --- add_memory / update_memory / supersede_memory ---
#
# Dedup is the agent's responsibility: it searches existing memories first and
# chooses add vs update vs supersede.


class AddMemoryInput(BaseModel):
    about_user_id: UUID | None  # None = couple-level
    content: str
    related_theme_ids: list[UUID] = []


class AddMemoryOutput(WriteCreated):
    pass


class UpdateMemoryInput(BaseModel):
    memory_id: UUID
    content: str | None = None
    related_theme_ids: list[UUID] | None = None
    status: MemoryStatus | None = None


class UpdateMemoryOutput(WriteUpdated):
    pass


class SupersedeMemoryInput(BaseModel):
    old_memory_id: UUID
    new_content: str
    related_theme_ids: list[UUID] = []


class SupersedeMemoryOutput(BaseModel):
    action: Literal["superseded"] = "superseded"
    new_id: UUID
    old_id: UUID


# --- create_theme / update_theme ---


class CreateThemeInput(BaseModel):
    title: str
    description: str
    sentiment: ThemeSentiment = ThemeSentiment.mixed
    health: ThemeHealth = ThemeHealth.tender
    seed_observation_ids: list[UUID] = []
    seed_memory_ids: list[UUID] = []


class CreateThemeOutput(WriteCreated):
    pass


class UpdateThemeInput(BaseModel):
    theme_id: UUID
    title: str | None = None
    description: str | None = None
    status: ThemeStatus | None = None
    sentiment: ThemeSentiment | None = None
    health: ThemeHealth | None = None
    mark_reinforced: bool = Field(
        default=False,
        description="True when this update reflects fresh evidence the domain is still live (updates last_reinforced_at).",
    )


class UpdateThemeOutput(WriteUpdated):
    pass


# --- watch items ---


class AddWatchItemInput(BaseModel):
    owner_user_id: UUID
    content: str
    due_at: datetime | None = None
    related_theme_ids: list[UUID] = []


class AddWatchItemOutput(WriteCreated):
    pass


class UpdateWatchItemInput(BaseModel):
    watch_item_id: UUID
    content: str | None = None
    due_at: datetime | None = None
    related_theme_ids: list[UUID] | None = None


class UpdateWatchItemOutput(WriteUpdated):
    pass


class AddressWatchItemInput(BaseModel):
    watch_item_id: UUID
    addressing_note: str = Field(
        description="One sentence on how this got addressed: bot surfaced it, user resolved it, or situation changed."
    )


class AddressWatchItemOutput(BaseModel):
    action: Literal["addressed"] = "addressed"
    id: UUID
    addressed_at: datetime


# --- observations ---


class LogObservationInput(BaseModel):
    content: str
    about_user_id: UUID | None  # None = about the dynamic / pair
    confidence: Confidence
    related_theme_ids: list[UUID] = []
    supporting_message_ids: list[UUID] = []
    significance: Significance | None = Field(
        default=None,
        description="Optional inline score. If omitted, scoring runs as a separate Haiku call.",
    )


# Agent searches existing observations first and picks log vs update.
class LogObservationOutput(WriteCreated):
    pass


class UpdateObservationInput(BaseModel):
    observation_id: UUID
    content: str | None = None
    confidence: Confidence | None = None
    status: ObservationStatus | None = None
    related_theme_ids: list[UUID] | None = None


class UpdateObservationOutput(WriteUpdated):
    pass


# --- OOB ---


class AddOOBInput(BaseModel):
    owner_id: UUID
    sensitive_core: str
    shareable_context: str | None = None
    severity: OOBSeverity
    review_at: datetime | None = None


class AddOOBOutput(WriteCreated):
    pass


class UpdateOOBInput(BaseModel):
    oob_id: UUID
    sensitive_core: str | None = None
    shareable_context: str | None = None
    severity: OOBSeverity | None = None
    review_at: datetime | None = None


class UpdateOOBOutput(WriteUpdated):
    pass


class LiftOOBInput(BaseModel):
    oob_id: UUID
    note: str | None = None


class LiftOOBOutput(BaseModel):
    action: Literal["lifted"] = "lifted"
    id: UUID
    lifted_at: datetime


# --- scheduling ---


class ScheduleCheckinInput(BaseModel):
    user_id: UUID
    when: datetime = Field(description="Absolute time to fire. Caller must convert relative to absolute.")
    about_what: str
    reason: str = Field(description="Why the bot decided this check-in is worth scheduling. Logged for audit.")

    @field_validator("when")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("when must be timezone-aware")
        return value


class ScheduleCheckinOutput(BaseModel):
    action: Literal["scheduled"] = "scheduled"
    job_id: UUID
    superseded_job_id: UUID | None
    scheduled_for: datetime


class CancelScheduledCheckinInput(BaseModel):
    user_id: UUID


class CancelScheduledCheckinOutput(BaseModel):
    action: Literal["cancelled", "noop"]
    cancelled_job_id: UUID | None


# --- escalation ---


class EscalateToPartnerInput(BaseModel):
    from_user_id: UUID
    to_user_id: UUID
    content: str = Field(
        description="The bot-authored body. If outside the 24h WhatsApp window, the escalation template is used and `content` populates the {{3}} contextual line."
    )
    reason: str = Field(description="Why this clears the high bar. Logged distinctly.")
    is_crisis: bool = False


class EscalateToPartnerOutput(BaseModel):
    action: Literal["sent", "deferred"]
    outbound_message_id: UUID | None
    used_template: bool
    reason_if_deferred: str | None


# --- outbound message controls ---


class EditOutboundMessageInput(BaseModel):
    message_id: UUID = Field(description="Internal id of an outbound bot message to edit.")
    content: str = Field(
        min_length=1,
        max_length=2000,
        description="Replacement user-visible text. Must be safe to show to the original recipient.",
    )
    reason: str = Field(description="Why editing the already-sent message is better than sending a follow-up.")


class EditOutboundMessageOutput(BaseModel):
    action: Literal["edited", "blocked", "unsupported", "not_found"]
    message_id: UUID
    provider_message_id: str | None = None
    reason: str | None = None
    suggested_rewrite: str | None = None


class DeleteOutboundMessageInput(BaseModel):
    message_id: UUID = Field(description="Internal id of an outbound bot message to delete.")
    reason: str = Field(description="Why deleting the already-sent message is appropriate.")


class DeleteOutboundMessageOutput(BaseModel):
    action: Literal["deleted", "unsupported", "not_found"]
    message_id: UUID
    provider_message_id: str | None = None
    reason: str | None = None


class ReactToMessageInput(BaseModel):
    message_id: UUID = Field(description="Internal id of the message to react to.")
    emoji: str = Field(
        min_length=1,
        max_length=32,
        description="A single Unicode emoji reaction. Prefer precise, emotionally apt, non-obvious choices over generic defaults.",
    )
    reason: str = Field(description="Why this emoji precisely fits the meaning of the message.")


class ReactToMessageOutput(BaseModel):
    action: Literal["reacted", "unsupported", "not_found"]
    message_id: UUID
    provider_message_id: str | None = None
    emoji: str
    reason: str | None = None


# --- media explanation ---


class ExplainMediaItemInput(BaseModel):
    message_id: UUID = Field(description="Internal id of an image message to explain and persist.")
    reason: str | None = Field(
        default=None,
        description="Why this media item needs a fresh durable explanation now.",
    )


class ExplainMediaItemOutput(BaseModel):
    action: Literal["explained", "not_found", "unsupported", "blocked"]
    message_id: UUID
    media_type: str | None = None
    explanation: str | None = None
    reason: str | None = None


# --- feedback ---


class LogFeedbackInput(BaseModel):
    from_user_id: UUID
    target_type: Literal["message", "turn", "general"]
    target_id: UUID | None
    sentiment: FeedbackSentiment
    content: str | None = None
    source: Literal["conversational", "reaction"] = "conversational"


class LogFeedbackOutput(WriteCreated):
    pass


# --- incremental outbound delivery ---


class SendMessagePartInput(BaseModel):
    content: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "One coherent user-visible message part to attempt now. Do not include "
            "scratch notes, tool decisions, hidden reasoning, or a bundle of future parts."
        ),
    )
    metadata: dict[str, str] | None = Field(
        default=None,
        description="Optional observability hints such as kind, tone, sequence_role, or reason_for_sending_now.",
    )
    client_part_key: str | None = Field(
        default=None,
        max_length=120,
        description="Optional semantic hint from the model. The runtime generates the authoritative idempotency key.",
    )


class SendMessagePartOutput(BaseModel):
    status: Literal[
        "sent",
        "duplicate",
        "blocked",
        "withheld",
        "interrupted",
        "provider_failed",
        "not_enabled",
    ]
    part_key: str | None = None
    client_part_key: str | None = None
    message_id: UUID | None = None
    provider_message_id: str | None = None
    delivered_content: str | None = None
    visible_to_user: bool = False
    sent_so_far: list[str] = Field(default_factory=list)
    reason: str | None = None
    suggested_rewrite: str | None = None


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
#
# Single source of truth mapping tool name -> (input model, output model).
# The orchestrator uses this to validate LLM-produced tool calls and to render
# the JSON schema list passed to the Anthropic API.

TOOL_REGISTRY: dict[str, tuple[type[BaseModel], type]] = {
    # read
    "search_messages": (SearchMessagesInput, SearchMessagesOutput),
    "search_emojis": (SearchEmojisInput, SearchEmojisOutput),
    "recent_activity": (RecentActivityInput, RecentActivityOutput),
    "list_themes": (ListThemesInput, ListThemesOutput),
    "get_theme": (GetThemeInput, GetThemeOutput),
    "get_memories": (GetMemoriesInput, GetMemoriesOutput),
    "list_watch_items": (ListWatchItemsInput, ListWatchItemsOutput),
    "get_observations": (GetObservationsInput, GetObservationsOutput),
    "get_oob": (GetOOBInput, GetOOBOutput),
    "summarize_oob_topics": (SummarizeOOBTopicsInput, SummarizeOOBTopicsOutput),
    "check_oob": (CheckOOBInput, CheckOOBOutput),
    "get_self_model": (GetSelfModelInput, GetSelfModelOutput),
    "get_bot_actions": (GetBotActionsInput, GetBotActionsOutput),
    "send_message_part": (SendMessagePartInput, SendMessagePartOutput),
    "list_bridge_candidates": (ListBridgeCandidatesInput, ListBridgeCandidatesOutput),
    # write
    "update_user_style_notes": (UpdateUserStyleNotesInput, UpdateUserStyleNotesOutput),
    "update_cross_thread_sharing_default": (UpdateCrossThreadSharingDefaultInput, UpdateCrossThreadSharingDefaultOutput),
    "create_bridge_candidate": (CreateBridgeCandidateInput, CreateBridgeCandidateOutput),
    "update_bridge_candidate": (UpdateBridgeCandidateInput, UpdateBridgeCandidateOutput),
    "send_bridge_candidate": (SendBridgeCandidateInput, SendBridgeCandidateOutput),
    "add_memory": (AddMemoryInput, AddMemoryOutput),
    "update_memory": (UpdateMemoryInput, UpdateMemoryOutput),
    "supersede_memory": (SupersedeMemoryInput, SupersedeMemoryOutput),
    "create_theme": (CreateThemeInput, CreateThemeOutput),
    "update_theme": (UpdateThemeInput, UpdateThemeOutput),
    "add_watch_item": (AddWatchItemInput, AddWatchItemOutput),
    "update_watch_item": (UpdateWatchItemInput, UpdateWatchItemOutput),
    "address_watch_item": (AddressWatchItemInput, AddressWatchItemOutput),
    "log_observation": (LogObservationInput, LogObservationOutput),
    "update_observation": (UpdateObservationInput, UpdateObservationOutput),
    "add_oob": (AddOOBInput, AddOOBOutput),
    "update_oob": (UpdateOOBInput, UpdateOOBOutput),
    "lift_oob": (LiftOOBInput, LiftOOBOutput),
    "schedule_checkin": (ScheduleCheckinInput, ScheduleCheckinOutput),
    "cancel_scheduled_checkin": (CancelScheduledCheckinInput, CancelScheduledCheckinOutput),
    "escalate_to_partner": (EscalateToPartnerInput, EscalateToPartnerOutput),
    "edit_outbound_message": (EditOutboundMessageInput, EditOutboundMessageOutput),
    "delete_outbound_message": (DeleteOutboundMessageInput, DeleteOutboundMessageOutput),
    "react_to_message": (ReactToMessageInput, ReactToMessageOutput),
    "explain_media_item": (ExplainMediaItemInput, ExplainMediaItemOutput),
    "log_feedback": (LogFeedbackInput, LogFeedbackOutput),
}
