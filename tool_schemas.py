"""
Tool I/O schemas for the mediation assistant.

Every tool the agentic loop can call has an input model, an output model, and
explicit error/edge variants. The LLM sees the input schema; the orchestrator
validates against the output schema before returning. Keep all enums and types
here so there is one source of truth.

Pydantic v2.
"""

from __future__ import annotations

from datetime import date as dt_date, datetime, time as dt_time
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Shared enums and primitives
# ---------------------------------------------------------------------------


class TemporalReference(BaseModel):
    utc: str
    local: str
    timezone: str
    local_date: str
    local_time: str
    local_weekday: str
    local_day_label: str = Field(description="today, yesterday, tomorrow, N days ago, or an ISO date.")
    relative_to_now: str = Field(description="Human-relative age such as about 2 hours ago or in 3 days.")
    display: str = Field(description="Compact primary label, e.g. today 21:03 Berlin.")


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


class DistillationStatus(str, Enum):
    active = "active"
    revised = "revised"
    retired = "retired"
    invalidated = "invalidated"


class DistillationSensitivity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class DistillationVisibility(str, Enum):
    private = "private"
    dyad_shareable = "dyad_shareable"


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


class BridgeCandidatePartnerPath(str, Enum):
    """How a bridge candidate should be handled across the partner bridge."""

    # Keep the ready row in the target partner prompt/hot context until it is
    # substantively addressed; this does not proactively send it now.
    message_partner = "message_partner"
    # Coach the source user to discuss the bridge in person.
    coach_in_person = "coach_in_person"
    # Suggest a casual source-side share without creating target prompt pressure.
    casual_share = "casual_share"
    # Hold as source-side context/bookkeeping until a better opening appears.
    hold_for_context = "hold_for_context"
    # Ask the source user for explicit permission before creating a target-facing bridge.
    ask_permission = "ask_permission"
    # Audit-only path: record that the material should not become a bridge.
    do_not_bridge = "do_not_bridge"


class PerspectiveTemplate(str, Enum):
    nvc = "nvc"
    gottman = "gottman"
    ifs_parts = "ifs_parts"
    reflective_listener = "reflective_listener"
    devils_advocate = "devils_advocate"


Significance = Annotated[int, Field(ge=1, le=5)]


class DateRange(BaseModel):
    start: datetime | None = None
    end: datetime | None = None


def _require_timezone_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


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
    local_day: Literal["today", "yesterday"] | dt_date | None = Field(
        default=None,
        description="Search one local calendar day in the current user's timezone, e.g. today, yesterday, or 2026-05-06. Do not combine with date_range.",
    )
    timezone: str | None = Field(
        default=None,
        description="Optional timezone for local_day; defaults to the current user's timezone.",
    )
    text_contains: str | None = Field(
        default=None,
        description="Plain substring match. Case-insensitive. Empty string = no filter.",
    )
    limit: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def validate_temporal_filters(self) -> "SearchMessagesInput":
        if self.local_day is not None and self.date_range is not None:
            raise ValueError("Use either local_day or date_range, not both.")
        return self


class MessageHit(BaseModel):
    id: UUID
    sender_id: UUID | None
    sent_at: datetime
    sent_at_time: TemporalReference | None = None
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
    last_message_at_time: TemporalReference | None = None
    summary: str  # LLM-generated digest, prepared by the tool


class RecentActivityOutput(BaseModel):
    threads: list[ThreadDigest]
    period: DateRange
    period_time: dict[str, TemporalReference | None] | None = None


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
    last_reinforced_at_time: TemporalReference | None = None
    last_active_at_time: TemporalReference | None = None


class ListThemesOutput(BaseModel):
    themes: list[ThemeSummary]


class GetThemeInput(BaseModel):
    theme_id: UUID


class ThemeDetail(ThemeSummary):
    description: str
    first_seen_at: datetime
    first_seen_at_time: TemporalReference | None = None
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
    created_at_time: TemporalReference | None = None
    last_referenced_at_time: TemporalReference | None = None


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
    due_at_time: TemporalReference | None = None
    created_at_time: TemporalReference | None = None
    addressed_at_time: TemporalReference | None = None
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
    created_at_time: TemporalReference | None = None
    last_reinforced_at_time: TemporalReference | None = None
    surfaced_count: int


class GetObservationsOutput(BaseModel):
    observations: list[ObservationRow]


# --- get_distillations ---


def _has_supporting_links(model: BaseModel) -> bool:
    for field_name in (
        "related_memory_ids",
        "related_observation_ids",
        "related_theme_ids",
        "supporting_message_ids",
    ):
        if getattr(model, field_name, None):
            return True
    return False


def _has_distillation_shareable_summary(model: BaseModel) -> bool:
    summary = getattr(model, "shareable_summary", None)
    return bool(summary and summary.strip())


class DistillationEvidenceMixin(BaseModel):
    related_memory_ids: list[UUID] = Field(default_factory=list)
    related_observation_ids: list[UUID] = Field(default_factory=list)
    related_theme_ids: list[UUID] = Field(default_factory=list)
    supporting_message_ids: list[UUID] = Field(default_factory=list)


class DistillationRow(DistillationEvidenceMixin):
    id: UUID
    content: str
    confidence: Confidence
    status: DistillationStatus
    sensitivity: DistillationSensitivity
    visibility: DistillationVisibility
    shareable_summary: str | None = None
    source_user_ids: list[UUID] = Field(min_length=1)
    created_from_tool_call_id: UUID | None = None
    triggering_message_id: UUID | None = None
    supersedes_distillation_id: UUID | None = None
    superseded_by_distillation_id: UUID | None = None
    revision_note: str | None = None
    revision_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    revised_at: datetime | None = None
    retired_at: datetime | None = None
    created_at_time: TemporalReference | None = None
    updated_at_time: TemporalReference | None = None
    revised_at_time: TemporalReference | None = None
    retired_at_time: TemporalReference | None = None

    @model_validator(mode="after")
    def validate_distillation_row(self) -> "DistillationRow":
        if not _has_supporting_links(self):
            raise ValueError("distillations must link to at least one supporting memory, observation, theme, or message")
        if self.visibility == DistillationVisibility.dyad_shareable and not _has_distillation_shareable_summary(self):
            raise ValueError("dyad_shareable distillations require a non-empty shareable_summary")
        return self


class GetDistillationsInput(BaseModel):
    status: DistillationStatus = DistillationStatus.active
    source_user_id: UUID | None = Field(
        default=None,
        description="If set, return distillations whose source_user_ids include this user.",
    )
    related_theme_id: UUID | None = None
    related_memory_id: UUID | None = None
    related_observation_id: UUID | None = None
    supporting_message_id: UUID | None = None
    text_contains: str | None = Field(
        default=None,
        description="Case-insensitive search across content, shareable_summary, and revision_note.",
    )
    limit: int = Field(default=100, ge=1, le=500)


class GetDistillationsOutput(BaseModel):
    distillations: list[DistillationRow]


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
    created_at_time: TemporalReference | None = None
    review_at_time: TemporalReference | None = None


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
    distillation = "distillation"
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
    started_at_time: TemporalReference | None = None
    user_in_context: UUID | None
    triggered_by_message_id: UUID | None
    final_output_message_id: UUID | None
    triggering_content: str | None = None
    final_outbound_content: str | None = None
    reasoning: str
    tool_calls: list[dict]  # raw tool_calls rows; the LLM can read them
    audit_events: list[dict] = Field(
        default_factory=list,
        description="Queryable per-turn diagnostic events. Plain metadata is sanitized; raw sensitive text is not exposed.",
    )


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
    partner_path: BridgeCandidatePartnerPath
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
    partner_path: BridgeCandidatePartnerPath = Field(
        description="Partner bridge path. Use message_partner for a ready target-facing bridge that stays in the target prompt until addressed; use do_not_bridge for audit-only non-bridges."
    )
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
    partner_path: BridgeCandidatePartnerPath | None = None
    limit: int = Field(default=10, ge=1, le=50)


class ListBridgeCandidatesOutput(BaseModel):
    candidates: list[BridgeCandidate]
    truncated: bool = False


class UpdateBridgeCandidateInput(BaseModel):
    candidate_id: UUID
    kind: BridgeCandidateKind | None = None
    status: BridgeCandidateStatus | None = None
    sensitivity: BridgeCandidateSensitivity | None = None
    partner_path: BridgeCandidatePartnerPath | None = Field(
        default=None,
        description="Optional partner bridge path update. Runtime authorization determines which side may change it.",
    )
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


# --- distillations ---


class AddDistillationInput(DistillationEvidenceMixin):
    content: str = Field(min_length=1)
    confidence: Confidence = Confidence.medium
    sensitivity: DistillationSensitivity = DistillationSensitivity.medium
    visibility: DistillationVisibility = DistillationVisibility.private
    shareable_summary: str | None = None
    source_user_ids: list[UUID] = Field(min_length=1)
    triggering_message_id: UUID | None = None

    @model_validator(mode="after")
    def validate_distillation(self) -> "AddDistillationInput":
        if not _has_supporting_links(self):
            raise ValueError("distillations must link to at least one supporting memory, observation, theme, or message")
        if self.visibility == DistillationVisibility.dyad_shareable and not _has_distillation_shareable_summary(self):
            raise ValueError("dyad_shareable distillations require a non-empty shareable_summary")
        return self


class AddDistillationOutput(WriteCreated):
    pass


class UpdateDistillationInput(BaseModel):
    distillation_id: UUID
    content: str | None = Field(default=None, min_length=1)
    confidence: Confidence | None = None
    status: DistillationStatus | None = None
    sensitivity: DistillationSensitivity | None = None
    visibility: DistillationVisibility | None = None
    shareable_summary: str | None = None
    source_user_ids: list[UUID] | None = Field(default=None, min_length=1)
    related_memory_ids: list[UUID] | None = None
    related_observation_ids: list[UUID] | None = None
    related_theme_ids: list[UUID] | None = None
    supporting_message_ids: list[UUID] | None = None
    revision_note: str | None = None

    @model_validator(mode="after")
    def validate_distillation_update(self) -> "UpdateDistillationInput":
        if self.status == DistillationStatus.revised:
            raise ValueError("use revise_distillation to mark a distillation revised")
        evidence_fields = (
            self.related_memory_ids,
            self.related_observation_ids,
            self.related_theme_ids,
            self.supporting_message_ids,
        )
        if any(field is not None for field in evidence_fields) and not any(evidence_fields):
            raise ValueError("distillation updates cannot clear all supporting links")
        if self.visibility == DistillationVisibility.dyad_shareable and not _has_distillation_shareable_summary(self):
            raise ValueError("dyad_shareable distillations require a non-empty shareable_summary")
        return self


class UpdateDistillationOutput(WriteUpdated):
    pass


class ReviseDistillationInput(DistillationEvidenceMixin):
    old_distillation_id: UUID
    new_content: str = Field(min_length=1)
    confidence: Confidence = Confidence.medium
    sensitivity: DistillationSensitivity = DistillationSensitivity.medium
    visibility: DistillationVisibility = DistillationVisibility.private
    shareable_summary: str | None = None
    source_user_ids: list[UUID] = Field(min_length=1)
    revision_note: str = Field(min_length=1)
    triggering_message_id: UUID | None = None

    @model_validator(mode="after")
    def validate_revision(self) -> "ReviseDistillationInput":
        if not _has_supporting_links(self):
            raise ValueError("distillation revisions must link to at least one supporting memory, observation, theme, or message")
        if self.visibility == DistillationVisibility.dyad_shareable and not _has_distillation_shareable_summary(self):
            raise ValueError("dyad_shareable distillations require a non-empty shareable_summary")
        return self


class ReviseDistillationOutput(BaseModel):
    action: Literal["revised"] = "revised"
    new_id: UUID
    old_id: UUID


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


class ScheduledTaskRecurrence(BaseModel):
    version: Literal[1] = 1
    type: Literal["hourly", "daily", "weekly"]
    interval: int = Field(default=1, ge=1)
    weekdays: list[int] | None = Field(
        default=None,
        description="Weekly rules only. Integers use Python weekday numbering: Monday=0 through Sunday=6.",
    )
    until: datetime | None = Field(
        default=None,
        description="Optional inclusive UTC end bound. Must be timezone-aware.",
    )
    remaining_occurrences: int | None = Field(
        default=None,
        ge=1,
        description="Optional total occurrences remaining, including the next scheduled fire.",
    )

    @field_validator("until")
    @classmethod
    def require_until_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, "recurrence.until")

    @model_validator(mode="after")
    def validate_rule_shape(self) -> "ScheduledTaskRecurrence":
        if self.type == "weekly":
            if not self.weekdays:
                raise ValueError("weekly recurrence requires weekdays")
            if any(day < 0 or day > 6 for day in self.weekdays):
                raise ValueError("recurrence.weekdays values must be between 0 and 6")
        elif self.weekdays:
            raise ValueError("hourly and daily recurrence must not set weekdays")
        return self


class ScheduledTaskRow(BaseModel):
    task_id: UUID
    job_id: UUID
    brief: str
    scheduled_for: datetime
    scheduled_for_time: TemporalReference | None = None
    recurrence: ScheduledTaskRecurrence | None = None
    recurrence_until_time: TemporalReference | None = Field(
        default=None,
        description="Relative/local rendering of recurrence.until when the task has a bounded recurrence.",
    )
    delayed: bool = False
    created_at: datetime | None = None
    created_at_time: TemporalReference | None = None


class ScheduleDelay(BaseModel):
    weeks: int = Field(default=0, ge=0, le=52)
    days: int = Field(default=0, ge=0, le=366)
    hours: int = Field(default=0, ge=0, le=8784)
    minutes: int = Field(default=0, ge=0, le=527040)

    @model_validator(mode="after")
    def validate_positive_duration(self) -> "ScheduleDelay":
        if self.weeks == 0 and self.days == 0 and self.hours == 0 and self.minutes == 0:
            raise ValueError("delay must be a positive duration")
        return self


class LocalScheduleTime(BaseModel):
    date: dt_date = Field(description="Local calendar date for the user's intended wall-clock time.")
    time: dt_time = Field(description="Local wall-clock time on that date, e.g. 21:00:00 for 9pm.")
    timezone: str | None = Field(
        default=None,
        description="IANA timezone for the local wall-clock time. Omit to use the current user's timezone.",
    )

    @field_validator("time")
    @classmethod
    def reject_timezone_on_time(cls, value: dt_time) -> dt_time:
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("local_when.time must not include a timezone")
        return value


class ScheduleTaskInput(BaseModel):
    brief: str = Field(min_length=1, max_length=2000)
    delay: ScheduleDelay | None = Field(
        default=None,
        description="Preferred/default for simple relative duration requests like 'in two hours', 'in 10 hours', or 'in two days'. Relative offset from the current server time. Provide exactly one of delay, local_when, or when.",
    )
    local_when: LocalScheduleTime | None = Field(
        default=None,
        description="Use for concrete local clock phrases like '9pm tonight', 'Monday at 8', or 'tomorrow morning'. The server converts this wall-clock time from the provided timezone, or the current user's timezone if omitted, to UTC.",
    )
    when: datetime | None = Field(
        default=None,
        description="Absolute exact instant. Do not use for user-local clock phrases; use local_when instead. If the current user is not in UTC, UTC/Z datetimes may be rejected so local-time mistakes can be corrected.",
    )
    recurrence: ScheduledTaskRecurrence | None = None

    @field_validator("when")
    @classmethod
    def require_when_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, "when")

    @model_validator(mode="after")
    def validate_schedule_time(self) -> "ScheduleTaskInput":
        if sum(value is not None for value in (self.when, self.delay, self.local_when)) != 1:
            raise ValueError("provide exactly one of when, delay, or local_when")
        return self


class ScheduleTaskOutput(BaseModel):
    action: Literal["scheduled"] = "scheduled"
    task_id: UUID
    job_id: UUID
    scheduled_for: datetime
    recurrence: ScheduledTaskRecurrence | None = None


class ListScheduledTasksInput(BaseModel):
    include_recurring: bool = True
    limit: int = Field(default=50, ge=1, le=200)


class ListScheduledTasksOutput(BaseModel):
    tasks: list[ScheduledTaskRow]


class UpdateScheduledTaskInput(BaseModel):
    task_id: UUID | None = None
    job_id: UUID | None = None
    current_task: bool = Field(
        default=False,
        description="Only valid during a scheduled_task turn; targets the currently firing task.",
    )
    brief: str | None = Field(default=None, min_length=1, max_length=2000)
    delay: ScheduleDelay | None = Field(
        default=None,
        description="Preferred/default replacement time for simple relative duration requests like 'in two hours', 'in 10 hours', or 'in two days'. Relative offset from the current server time. Do not provide together with when or local_when.",
    )
    local_when: LocalScheduleTime | None = Field(
        default=None,
        description="Replacement local wall-clock time for phrases like '9pm tonight' or 'Monday at 8'. The server converts from the provided timezone, or the current user's timezone if omitted, to UTC.",
    )
    when: datetime | None = Field(
        default=None,
        description="Replacement exact instant. Do not use for user-local clock phrases; use local_when instead. If the current user is not in UTC, UTC/Z datetimes may be rejected so local-time mistakes can be corrected.",
    )
    recurrence: ScheduledTaskRecurrence | None = Field(
        default=None,
        description="Replacement recurrence. Explicit null makes the task one-shot.",
    )
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("when")
    @classmethod
    def require_update_when_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, "when")

    @model_validator(mode="after")
    def validate_update_target_and_payload(self) -> "UpdateScheduledTaskInput":
        targets = [self.task_id is not None, self.job_id is not None, self.current_task]
        if sum(targets) != 1:
            raise ValueError("provide exactly one of task_id, job_id, or current_task=true")
        has_recurrence_update = "recurrence" in self.model_fields_set
        if sum(value is not None for value in (self.when, self.delay, self.local_when)) > 1:
            raise ValueError("provide at most one of when, delay, or local_when")
        if self.brief is None and self.when is None and self.delay is None and self.local_when is None and not has_recurrence_update:
            raise ValueError("provide at least one update: brief, when, local_when, or recurrence")
        return self


class UpdateScheduledTaskOutput(BaseModel):
    action: Literal["updated", "noop"]
    task_id: UUID | None = None
    job_id: UUID | None = None
    scheduled_for: datetime | None = None
    recurrence: ScheduledTaskRecurrence | None = None


class CancelScheduledTaskInput(BaseModel):
    task_id: UUID | None = None
    job_id: UUID | None = None
    current_task: bool = Field(
        default=False,
        description="Only valid during a scheduled_task turn; targets the currently firing task.",
    )
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_cancel_target(self) -> "CancelScheduledTaskInput":
        targets = [self.task_id is not None, self.job_id is not None, self.current_task]
        if sum(targets) != 1:
            raise ValueError("provide exactly one of task_id, job_id, or current_task=true")
        return self


class CancelScheduledTaskOutput(BaseModel):
    action: Literal["cancelled", "noop"]
    task_id: UUID | None = None
    job_id: UUID | None = None


class ScheduleCheckinInput(BaseModel):
    user_id: UUID
    delay: ScheduleDelay | None = Field(
        default=None,
        description="Preferred/default for simple relative duration requests like 'in two hours', 'in 10 hours', or 'in two days'. Relative offset from the current server time. Provide exactly one of delay, local_when, or when.",
    )
    local_when: LocalScheduleTime | None = Field(
        default=None,
        description="Use for concrete local clock phrases like '9pm tonight', 'Monday at 8', or 'tomorrow morning'. The server converts this wall-clock time from the provided timezone, or the current user's timezone if omitted, to UTC.",
    )
    when: datetime | None = Field(
        default=None,
        description="Absolute exact instant. Do not use for user-local clock phrases; use local_when instead. If the current user is not in UTC, UTC/Z datetimes may be rejected so local-time mistakes can be corrected.",
    )
    about_what: str
    reason: str = Field(description="Why the bot decided this check-in is worth scheduling. Logged for audit.")

    @field_validator("when")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, "when")

    @model_validator(mode="after")
    def validate_schedule_time(self) -> "ScheduleCheckinInput":
        if sum(value is not None for value in (self.when, self.delay, self.local_when)) != 1:
            raise ValueError("provide exactly one of when, delay, or local_when")
        return self


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


# --- consult_perspective ---


class ConsultPerspectiveInput(BaseModel):
    template: PerspectiveTemplate | None = None
    perspective: str | None = Field(default=None, min_length=1, max_length=1000)
    focus: str = Field(min_length=1, max_length=1500)
    proposed_response: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def require_one_perspective_source(self) -> "ConsultPerspectiveInput":
        if (self.template is None) == (self.perspective is None):
            raise ValueError("provide exactly one of template or perspective")
        return self


class ConsultPerspectiveOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    summary: str | None = None
    key_points: list[str] = Field(default_factory=list)
    suggested_moves: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    confidence: Confidence | None = None
    template_used: PerspectiveTemplate | str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> "ConsultPerspectiveOutput":
        if self.is_error:
            if not self.error:
                raise ValueError("error is required when is_error is true")
            return self
        if not self.summary:
            raise ValueError("summary is required when is_error is false")
        if self.confidence is None:
            raise ValueError("confidence is required when is_error is false")
        return self


# --- adaptive turn plan ---


TurnStepValue = Literal["read", "consult", "respond", "record", "schedule", "done"]


class UpdateTurnPlanInput(BaseModel):
    add_steps: list[TurnStepValue] | None = Field(
        default=None,
        description="Steps to add before done when the current turn needs more work than the initial skeleton.",
    )
    remove_steps: list[TurnStepValue] | None = Field(
        default=None,
        description="Steps to remove when they are unnecessary. The runtime always keeps done.",
    )
    mark_done: list[TurnStepValue] | None = Field(
        default=None,
        description="Steps that should be marked complete in the visible turn checklist.",
    )
    note: str | None = Field(default=None, max_length=500, description="A compact private note explaining the change.")


class UpdateTurnPlanOutput(BaseModel):
    plan: str
    current: TurnStepValue
    steps: list[TurnStepValue]
    completed: list[TurnStepValue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
#
# Single source of truth mapping tool name -> (input model, output model).
# The orchestrator uses this to validate LLM-produced tool calls and to render
# the JSON schema list passed to the Anthropic API.

TOOL_REGISTRY: dict[str, tuple[type[BaseModel], type]] = {
    "update_turn_plan": (UpdateTurnPlanInput, UpdateTurnPlanOutput),
    # read
    "search_messages": (SearchMessagesInput, SearchMessagesOutput),
    "search_emojis": (SearchEmojisInput, SearchEmojisOutput),
    "recent_activity": (RecentActivityInput, RecentActivityOutput),
    "list_themes": (ListThemesInput, ListThemesOutput),
    "get_theme": (GetThemeInput, GetThemeOutput),
    "get_memories": (GetMemoriesInput, GetMemoriesOutput),
    "list_watch_items": (ListWatchItemsInput, ListWatchItemsOutput),
    "get_observations": (GetObservationsInput, GetObservationsOutput),
    "get_distillations": (GetDistillationsInput, GetDistillationsOutput),
    "get_oob": (GetOOBInput, GetOOBOutput),
    "summarize_oob_topics": (SummarizeOOBTopicsInput, SummarizeOOBTopicsOutput),
    "check_oob": (CheckOOBInput, CheckOOBOutput),
    "get_self_model": (GetSelfModelInput, GetSelfModelOutput),
    "get_bot_actions": (GetBotActionsInput, GetBotActionsOutput),
    "send_message_part": (SendMessagePartInput, SendMessagePartOutput),
    "consult_perspective": (ConsultPerspectiveInput, ConsultPerspectiveOutput),
    "list_bridge_candidates": (ListBridgeCandidatesInput, ListBridgeCandidatesOutput),
    "list_scheduled_tasks": (ListScheduledTasksInput, ListScheduledTasksOutput),
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
    "add_distillation": (AddDistillationInput, AddDistillationOutput),
    "update_distillation": (UpdateDistillationInput, UpdateDistillationOutput),
    "revise_distillation": (ReviseDistillationInput, ReviseDistillationOutput),
    "add_oob": (AddOOBInput, AddOOBOutput),
    "update_oob": (UpdateOOBInput, UpdateOOBOutput),
    "lift_oob": (LiftOOBInput, LiftOOBOutput),
    "schedule_checkin": (ScheduleCheckinInput, ScheduleCheckinOutput),
    "cancel_scheduled_checkin": (CancelScheduledCheckinInput, CancelScheduledCheckinOutput),
    "schedule_task": (ScheduleTaskInput, ScheduleTaskOutput),
    "update_scheduled_task": (UpdateScheduledTaskInput, UpdateScheduledTaskOutput),
    "cancel_scheduled_task": (CancelScheduledTaskInput, CancelScheduledTaskOutput),
    "escalate_to_partner": (EscalateToPartnerInput, EscalateToPartnerOutput),
    "edit_outbound_message": (EditOutboundMessageInput, EditOutboundMessageOutput),
    "delete_outbound_message": (DeleteOutboundMessageInput, DeleteOutboundMessageOutput),
    "react_to_message": (ReactToMessageInput, ReactToMessageOutput),
    "explain_media_item": (ExplainMediaItemInput, ExplainMediaItemOutput),
    "log_feedback": (LogFeedbackInput, LogFeedbackOutput),
}
