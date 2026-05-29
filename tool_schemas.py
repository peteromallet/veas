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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.live.schemas import Agenda


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
    local_day_label: str = Field(
        description="today, yesterday, tomorrow, N days ago, or an ISO date."
    )
    relative_to_now: str = Field(
        description="Human-relative age such as about 2 hours ago or in 3 days."
    )
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


class PartnerShare(str, Enum):
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
    scope: Literal["own", "all"] = "own"
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
    is_error: bool = False
    error: str | None = None
    themes: list[ThemeSummary]


class GetThemeInput(BaseModel):
    scope: Literal["own", "all"] = "own"
    theme_id: UUID


class ThemeDetail(ThemeSummary):
    description: str
    first_seen_at: datetime
    first_seen_at_time: TemporalReference | None = None
    related_memory_ids: list[UUID]
    related_observation_ids: list[UUID]


class GetThemeOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    theme: ThemeDetail | None  # None if not found


# --- get_memories ---


class GetMemoriesInput(BaseModel):
    scope: Literal["own", "all"] = "own"
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
    visibility: DistillationVisibility = DistillationVisibility.private
    shareable_summary: str | None = None
    related_theme_ids: list[UUID]
    created_at: datetime
    last_referenced_at: datetime | None
    created_at_time: TemporalReference | None = None
    last_referenced_at_time: TemporalReference | None = None


class GetMemoriesOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    memories: list[MemoryRow]


# --- list_watch_items ---


class ListWatchItemsInput(BaseModel):
    scope: Literal["own", "all"] = "own"
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
    is_error: bool = False
    error: str | None = None
    items: list[WatchItemRow]


# --- get_observations ---


class GetObservationsInput(BaseModel):
    scope: Literal["own", "all"] = "own"
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
    is_error: bool = False
    error: str | None = None
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
            raise ValueError(
                "distillations must link to at least one supporting memory, observation, theme, or message"
            )
        if (
            self.visibility == DistillationVisibility.dyad_shareable
            and not _has_distillation_shareable_summary(self)
        ):
            raise ValueError(
                "dyad_shareable distillations require a non-empty shareable_summary"
            )
        return self


class GetDistillationsInput(BaseModel):
    scope: Literal["own", "all"] = "own"
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
    is_error: bool = False
    error: str | None = None
    distillations: list[DistillationRow]


# --- get_oob ---


class GetOOBInput(BaseModel):
    scope: Literal["own", "all"] = "own"
    owner_id: UUID | None = None  # None = both partners
    include_lifted: bool = False


class OOBRow(BaseModel):
    id: UUID
    owner_id: UUID
    protected_summary: str = Field(
        description="Safe, non-sensitive display text; never the raw protected core."
    )
    shareable_context: str | None
    severity: OOBSeverity
    status: OOBStatus
    created_at: datetime
    review_at: datetime | None
    created_at_time: TemporalReference | None = None
    review_at_time: TemporalReference | None = None


class GetOOBOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    entries: list[OOBRow]


# --- summarize_oob_topics ---


class SummarizeOOBTopicsInput(BaseModel):
    scope: Literal["own", "all"] = "own"
    owner_id: UUID = Field(
        description="The partner whose active OOB topic categories should be summarized."
    )


class OOBTopicCluster(BaseModel):
    count: int = Field(ge=1)
    topic: str


class SummarizeOOBTopicsOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
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
    scope: Literal["own", "all"] = "own"
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
    is_error: bool = False
    error: str | None = None
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
    handling_result: str | None = None
    processing_error: str | None = None


class GetBotActionsOutput(BaseModel):
    actions: list[BotAction]


# --- get_tool_call ---

class GetToolCallInput(BaseModel):
    tool_call_id: UUID = Field(
        description="The id of the tool_calls row to fetch — usually surfaced from a recent-turn summary or get_bot_actions output."
    )


class ToolCallDetail(BaseModel):
    id: UUID
    turn_id: UUID
    tool_name: str
    kind: str
    summary: str | None
    arguments: dict
    result: dict
    called_at: datetime
    called_at_time: TemporalReference | None = None
    duration_ms: int | None


class GetToolCallOutput(BaseModel):
    tool_call: ToolCallDetail | None = None


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


# --- set_partner_sharing ---


class SetPartnerSharingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opt_in: bool = Field(
        description="Whether this bot may share this user's safe, bot-specific dyad_shareable rows with their partner."
    )
    reason: str | None = Field(
        default=None,
        description="Optional explicit user rationale or short note for audit. Do not infer a choice from vague comfort or discomfort.",
    )


class SetPartnerSharingOutput(BaseModel):
    user_id: UUID
    bot_id: str
    partner_share: PartnerShare
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
    scope: Literal["own", "all"] = "own"
    source_user_id: UUID | None = None
    target_user_id: UUID | None = None
    status: BridgeCandidateStatus | None = None
    partner_path: BridgeCandidatePartnerPath | None = None
    limit: int = Field(default=10, ge=1, le=50)


class ListBridgeCandidatesOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
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
    visibility: DistillationVisibility = DistillationVisibility.private
    shareable_summary: str | None = None
    related_theme_ids: list[UUID] = []
    topic_slugs: list[str] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_memory(self) -> "AddMemoryInput":
        if (
            self.visibility == DistillationVisibility.dyad_shareable
            and not _has_distillation_shareable_summary(self)
        ):
            raise ValueError(
                "dyad_shareable memories require a non-empty shareable_summary"
            )
        return self


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
    topic_slugs: list[str] | None = None
    reason: str | None = None


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
    topic_slugs: list[str] | None = None
    reason: str | None = None


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
    topic_slugs: list[str] | None = None
    reason: str | None = None


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
    topic_slugs: list[str] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_distillation(self) -> "AddDistillationInput":
        if not _has_supporting_links(self):
            raise ValueError(
                "distillations must link to at least one supporting memory, observation, theme, or message"
            )
        if (
            self.visibility == DistillationVisibility.dyad_shareable
            and not _has_distillation_shareable_summary(self)
        ):
            raise ValueError(
                "dyad_shareable distillations require a non-empty shareable_summary"
            )
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
        if any(field is not None for field in evidence_fields) and not any(
            evidence_fields
        ):
            raise ValueError("distillation updates cannot clear all supporting links")
        if (
            self.visibility == DistillationVisibility.dyad_shareable
            and not _has_distillation_shareable_summary(self)
        ):
            raise ValueError(
                "dyad_shareable distillations require a non-empty shareable_summary"
            )
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
            raise ValueError(
                "distillation revisions must link to at least one supporting memory, observation, theme, or message"
            )
        if (
            self.visibility == DistillationVisibility.dyad_shareable
            and not _has_distillation_shareable_summary(self)
        ):
            raise ValueError(
                "dyad_shareable distillations require a non-empty shareable_summary"
            )
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
    topic_slugs: list[str] | None = None
    reason: str | None = None


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
    date: dt_date = Field(
        description="Local calendar date for the user's intended wall-clock time."
    )
    time: dt_time = Field(
        description="Local wall-clock time on that date, e.g. 21:00:00 for 9pm."
    )
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
        if (
            sum(value is not None for value in (self.when, self.delay, self.local_when))
            != 1
        ):
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
            raise ValueError(
                "provide exactly one of task_id, job_id, or current_task=true"
            )
        has_recurrence_update = "recurrence" in self.model_fields_set
        if (
            sum(value is not None for value in (self.when, self.delay, self.local_when))
            > 1
        ):
            raise ValueError("provide at most one of when, delay, or local_when")
        if (
            self.brief is None
            and self.when is None
            and self.delay is None
            and self.local_when is None
            and not has_recurrence_update
        ):
            raise ValueError(
                "provide at least one update: brief, when, local_when, or recurrence"
            )
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
            raise ValueError(
                "provide exactly one of task_id, job_id, or current_task=true"
            )
        return self


class CancelScheduledTaskOutput(BaseModel):
    action: Literal["cancelled", "noop"]
    task_id: UUID | None = None
    job_id: UUID | None = None


class UpdateScheduledCheckinInput(BaseModel):
    job_id: UUID
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
    about_what: str | None = Field(default=None, min_length=1, max_length=2000)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("when")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, "when")

    @model_validator(mode="after")
    def validate_update_target_and_payload(self) -> "UpdateScheduledCheckinInput":
        if (
            sum(value is not None for value in (self.when, self.delay, self.local_when))
            > 1
        ):
            raise ValueError("provide at most one of when, delay, or local_when")
        if (
            self.about_what is None
            and self.reason is None
            and self.when is None
            and self.delay is None
            and self.local_when is None
        ):
            raise ValueError(
                "provide at least one update: about_what, reason, when, delay, or local_when"
            )
        return self


class UpdateScheduledCheckinOutput(BaseModel):
    action: Literal["updated", "noop"]
    job_id: UUID
    scheduled_for: datetime | None = None
    about_what: str | None = None


class ListAllRemindersInput(BaseModel):
    """No input fields — scoped to (user_id, bot_id, topic_id) server-side."""


class ReminderItem(BaseModel):
    id: UUID
    kind: Literal["task", "checkin"]
    next_fire_local: str
    next_fire_utc: datetime
    recurrence_label: str
    recurrence_rule: dict | None = None
    brief: str | None = None
    about_what: str | None = None
    reason: str | None = None


class ListAllRemindersOutput(BaseModel):
    items: list[ReminderItem]


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
    reason: str = Field(
        description="Why the bot decided this check-in is worth scheduling. Logged for audit."
    )

    @field_validator("when")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, "when")

    @model_validator(mode="after")
    def validate_schedule_time(self) -> "ScheduleCheckinInput":
        if (
            sum(value is not None for value in (self.when, self.delay, self.local_when))
            != 1
        ):
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


# --- partner-nudge (SD-001..SD-014) ---
#
# Cross-partner check-in nudge primitive. Schema invariants (load-bearing):
#   * NO ``user_id`` / ``target_user_id`` field on the Input. Backend
#     resolves the partner via ``resolve_dyad_partner`` so a hallucinated
#     id cannot redirect a write at a stranger.
#   * ``nudge_note`` is recipient-visible (rendered in the partner's hot
#     context). ``reason`` is audit-only and is NEVER rendered.
#   * ``source`` is telemetry — bot-judgment autonomous nudges ship inert
#     in this release.


class SchedulePartnerCheckinInput(BaseModel):
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
    nudge_note: str | None = Field(
        default=None,
        max_length=300,
        description="Optional recipient-visible note. Keep neutral and short ('Pom asked me to see how you're doing today.'); never quote the originator's private words or summarize private content.",
    )
    reason: str = Field(
        description="Why the originator decided this nudge is worth scheduling. Logged for audit only — NEVER rendered into any prompt or hot context.",
    )
    source: Literal["explicit_user_request", "bot_judgment"] = Field(
        default="explicit_user_request",
        description="Set to 'explicit_user_request' for direct user words like 'check in on Hannah'. 'bot_judgment' is reserved for autonomous nudges (not enabled in this release).",
    )

    @field_validator("when")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, "when")

    @model_validator(mode="after")
    def validate_schedule_time(self) -> "SchedulePartnerCheckinInput":
        if (
            sum(value is not None for value in (self.when, self.delay, self.local_when))
            != 1
        ):
            raise ValueError("provide exactly one of when, delay, or local_when")
        return self


class SchedulePartnerCheckinOutput(BaseModel):
    action: Literal["scheduled"] = "scheduled"
    job_id: UUID
    scheduled_for: datetime
    recipient_user_id: UUID


class CancelPartnerNudgeInput(BaseModel):
    job_id: UUID


class CancelPartnerNudgeOutput(BaseModel):
    action: Literal["cancelled", "noop"]
    cancelled_job_id: UUID | None


# --- list_scheduled_checkins (SD-014) ---
#
# Symmetric to list_scheduled_tasks but for user-facing check-ins.
# Returns ONLY pending checkin rows scoped to ctx.user.id × ctx.bot_id
# (so a user with both mediator and Tante Rosi check-ins sees only the
# current bot's). Mirrors ScheduledTaskRow minus recurrence fields —
# check-ins are one-off by design.


class ScheduledCheckinRow(BaseModel):
    job_id: UUID
    bot_id: str | None = None
    topic_id: UUID | None = None
    scheduled_for: datetime
    scheduled_for_time: TemporalReference | None = None
    about_what: str | None = None
    reason: str | None = None
    created_at: datetime | None = None
    created_at_time: TemporalReference | None = None


class ListScheduledCheckinsInput(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


class ListScheduledCheckinsOutput(BaseModel):
    checkins: list[ScheduledCheckinRow]


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
    message_id: UUID = Field(
        description="Internal id of an outbound bot message to edit."
    )
    content: str = Field(
        min_length=1,
        max_length=2000,
        description="Replacement user-visible text. Must be safe to show to the original recipient.",
    )
    reason: str = Field(
        description="Why editing the already-sent message is better than sending a follow-up."
    )


class EditOutboundMessageOutput(BaseModel):
    action: Literal["edited", "blocked", "unsupported", "not_found"]
    message_id: UUID
    provider_message_id: str | None = None
    reason: str | None = None
    suggested_rewrite: str | None = None


class DeleteOutboundMessageInput(BaseModel):
    message_id: UUID = Field(
        description="Internal id of an outbound bot message to delete."
    )
    reason: str = Field(
        description="Why deleting the already-sent message is appropriate."
    )


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
    reason: str = Field(
        description="Why this emoji precisely fits the meaning of the message."
    )


class ReactToMessageOutput(BaseModel):
    action: Literal["reacted", "unsupported", "not_found"]
    message_id: UUID
    provider_message_id: str | None = None
    emoji: str
    reason: str | None = None


# --- media explanation ---


class ExplainMediaItemInput(BaseModel):
    message_id: UUID = Field(
        description="Internal id of an image message to explain and persist."
    )
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
    note: str | None = Field(
        default=None,
        max_length=500,
        description="A compact private note explaining the change.",
    )


class UpdateTurnPlanOutput(BaseModel):
    plan: str
    current: TurnStepValue
    steps: list[TurnStepValue]
    completed: list[TurnStepValue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pregnancy write tools (Tante Rosi — solo pregnancy coach)
# ---------------------------------------------------------------------------


class SetPregnancyEddInput(BaseModel):
    edd: str = Field(
        description="Estimated due date as ISO date string, e.g. '2026-10-22'."
    )
    dating_basis: Literal["lmp", "scan"] = Field(
        description="How the EDD was determined: 'lmp' (last menstrual period) or 'scan' (dating ultrasound)."
    )
    lmp_date: str | None = Field(
        default=None,
        description="First day of last menstrual period as ISO date, e.g. '2026-01-15'.",
    )
    scan_date: str | None = Field(
        default=None,
        description="Date of the dating scan as ISO date, e.g. '2026-03-01'.",
    )
    started_at: str | None = Field(
        default=None,
        description="When the user started tracking this pregnancy as ISO datetime. Defaults to now().",
    )


class SetPregnancyEddOutput(BaseModel):
    ok: bool
    edd: str
    gestational_age: str


class CorrectPregnancyEddInput(BaseModel):
    edd: str = Field(description="Revised estimated due date as ISO date string.")
    dating_basis: Literal["lmp", "scan"] = Field(
        description="How the revised EDD was determined."
    )
    scan_date: str | None = Field(
        default=None,
        description="Date of the corrective scan as ISO date, e.g. '2026-04-15'.",
    )


class CorrectPregnancyEddOutput(BaseModel):
    ok: bool
    edd: str
    gestational_age: str


class EndPregnancyInput(BaseModel):
    outcome: Literal["birth", "loss", "termination"] = Field(
        description="How the pregnancy concluded."
    )
    ended_at: str | None = Field(
        default=None,
        description="When the pregnancy ended as ISO datetime. Defaults to now() if omitted.",
    )


class EndPregnancyOutput(BaseModel):
    ok: bool
    outcome: str
    ended_at: str


# ---------------------------------------------------------------------------
# Hector fitness tools — commitment/adherence substrate
# ---------------------------------------------------------------------------


# ── Hector enums (must match migration CHECK constraints exactly) ──────


class CommitmentStatus(str, Enum):
    active = "active"
    paused = "paused"
    completed = "completed"
    dropped = "dropped"


class Cadence(str, Enum):
    daily = "daily"
    weekdays = "weekdays"
    weekly_count = "weekly_count"
    custom = "custom"
    custom_days = "custom_days"


class PressureStyle(str, Enum):
    very_gentle = "very_gentle"
    low_key = "low_key"
    firm = "firm"


class AdherenceStatus(str, Enum):
    done = "done"
    missed = "missed"
    excused = "excused"


class ScheduleRule(BaseModel):
    """Small structured schedule details stored as JSONB in commitments."""

    period: str = "week"
    days: list[int] = Field(default_factory=list)
    target_count: int = 1
    timezone: str = "UTC"


# --- create_commitment ---


class CreateCommitmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Short human-readable label for the commitment.")
    kind: str = Field(
        description="Free-form category for this commitment. Examples: workout, nutrition, sleep, mobility, meditation, screen_time, journaling, hydration, reading, body_measurement, other."
    )
    cadence: Cadence = Field(
        description="How often the commitment is expected."
    )
    days_of_week: list[int] | None = Field(
        default=None,
        description="For custom_days cadence: 0=Monday … 6=Sunday.",
    )
    target_count: int | None = Field(
        default=None,
        description="For weekly_count cadence: how many times per week.",
    )
    start_date: str | None = Field(
        default=None, description="ISO date string, e.g. '2026-05-11'. Defaults to today."
    )
    end_date: str | None = Field(
        default=None, description="ISO date string. Open-ended if omitted."
    )
    schedule_rule: ScheduleRule | None = Field(
        default=None,
        description="Optional structured schedule details (period, days, target_count, timezone).",
    )
    pressure_style: PressureStyle = Field(
        default=PressureStyle.low_key,
        description="How much encouragement/accountability pressure the bot should apply.",
    )

    @model_validator(mode="after")
    def validate_cadence_constraints(self) -> "CreateCommitmentInput":
        if self.cadence == Cadence.weekly_count and self.target_count is not None:
            if self.target_count < 1 or self.target_count > 7:
                raise ValueError("target_count must be between 1 and 7 for weekly_count cadence")
        if self.cadence == Cadence.custom_days and not self.days_of_week:
            raise ValueError("days_of_week is required when cadence is custom_days")
        return self


class CreateCommitmentOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    commitment_id: str | None = None
    label: str | None = None
    cadence: str | None = None
    created_at: str | None = None


# --- update_commitment ---


class UpdateCommitmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str = Field(description="UUID of the commitment to update.")
    label: str | None = None
    kind: str | None = None
    cadence: Cadence | None = None
    days_of_week: list[int] | None = None
    target_count: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    schedule_rule: ScheduleRule | None = None
    pressure_style: PressureStyle | None = None


class UpdateCommitmentOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    commitment_id: str | None = None
    updated_at: str | None = None


# --- close_commitment ---


class CloseCommitmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str = Field(description="UUID of the commitment to close.")
    status: Literal["paused", "completed", "dropped"] = Field(
        description="Final status: paused (temporary), completed (done), dropped (abandoned)."
    )


class CloseCommitmentOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    commitment_id: str | None = None
    status: str | None = None
    closed_at: str | None = None


# --- log_event ---


class LogEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str | None = Field(
        default=None,
        description="UUID of the related commitment, if this event satisfies/modifies one.",
    )
    metric_key: str = Field(
        description="Free-form key naming what was measured/logged. Examples: workout_session, ate_on_plan, meditation_session, screen_free_evening, body_weight, etc."
    )
    adherence_status: AdherenceStatus | None = Field(
        default=None,
        description="Adherence outcome. Required unless value_numeric or value_text is set.",
    )
    value_numeric: float | None = Field(
        default=None, description="Numeric measurement value."
    )
    value_text: str | None = Field(
        default=None, description="Free-text measurement value."
    )
    unit: str | None = Field(default=None, description="Unit for value_numeric.")
    observed_at: str | None = Field(
        default=None, description="ISO datetime. Defaults to now()."
    )
    note: str | None = Field(default=None, description="Optional context note.")
    source_message_ids: list[str] | None = Field(
        default=None, description="Message UUIDs that triggered this event."
    )

    @model_validator(mode="after")
    def require_at_least_one_value(self) -> "LogEventInput":
        if (
            self.adherence_status is None
            and self.value_numeric is None
            and self.value_text is None
        ):
            raise ValueError(
                "At least one of adherence_status, value_numeric, or value_text must be set."
            )
        return self


class LogEventOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    event_id: str | None = None
    commitment_id: str | None = None
    metric_key: str | None = None
    adherence_status: str | None = None
    observed_at: str | None = None


# --- list_commitments ---


class ListCommitmentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = Field(
        default="active",
        description="Filter by status: active, paused, completed, dropped. None = all.",
    )


class CommitmentSummary(BaseModel):
    id: str
    label: str
    kind: str
    status: str
    cadence: str
    days_of_week: list[int] = []
    target_count: int | None = None
    start_date: str
    end_date: str | None = None
    pressure_style: str
    created_at: str
    updated_at: str


class ListCommitmentsOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    commitments: list[CommitmentSummary] = []


# --- list_events ---


class ListEventsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str | None = Field(
        default=None, description="Filter to a specific commitment."
    )
    limit: int = Field(default=20, ge=1, le=200)
    before: str | None = Field(
        default=None, description="ISO datetime. Only events before this time."
    )


class EventSummary(BaseModel):
    id: str
    commitment_id: str | None = None
    metric_key: str
    adherence_status: str | None = None
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    observed_at: str
    note: str | None = None
    created_at: str


class ListEventsOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    events: list[EventSummary] = []


# --- get_adherence ---


class GetAdherenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_ids: list[str] | None = Field(
        default=None,
        description="Specific commitment UUIDs. None = all active commitments for this topic.",
    )


class AdherenceSlot(BaseModel):
    date: str
    day_label: str
    status: Literal["done", "missed", "excused", "unknown", "pending"]


class CommitmentAdherence(BaseModel):
    commitment_id: str
    label: str
    cadence: str
    slots: list[AdherenceSlot]
    summary: str


class GetAdherenceOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    commitments: list[CommitmentAdherence] = []
    week_start: str | None = None
    week_end: str | None = None


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
#
# ---------------------------------------------------------------------------
# submit_live_brief (S2 agentic live prep)
# ---------------------------------------------------------------------------


class SubmitLiveBriefInput(BaseModel):
    agenda: Agenda
    notes: str | None = None


class SubmitLiveBriefOutput(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------
# submit_live_debrief (S3 agentic live debrief)
# ---------------------------------------------------------------------------


class EvidenceReferenceV1(BaseModel):
    """Structured transcript evidence for a debrief claim."""
    model_config = ConfigDict(extra="allow")
    transcript_turn_id: str | None = None
    quote: str | None = None
    confidence: float | None = None


class FailedWriteV1(BaseModel):
    """Record of a failed durable write during debrief."""
    model_config = ConfigDict(extra="allow")
    tool_name: str | None = None
    reason: str | None = None
    evidence_refs: list[EvidenceReferenceV1] | None = None


class SubmitLiveDebriefInput(BaseModel):
    """Required finalization gate for live debrief.  The debrief model must call
    this exactly once before the tool cap, or the job is marked debrief_failed."""
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1

    # ── Core review fields ──────────────────────────────────────────────
    review_summary: str | None = None
    what_heard: str = ""
    what_decided: str = ""
    still_open: str = ""
    what_to_remember: str = ""

    # ── Durable write audit ─────────────────────────────────────────────
    durable_write_summary: str = ""
    open_questions: str = ""

    # ── Evidence and failure tracking ───────────────────────────────────
    references: list[EvidenceReferenceV1] | None = None
    failed_writes: list[FailedWriteV1] | None = None


class SubmitLiveDebriefOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool = True


# ---------------------------------------------------------------------------
# set_topic_status (S4)
# ---------------------------------------------------------------------------


class SetTopicStatusInput(BaseModel):
    scope: Literal["user", "dyad"]
    user_id: UUID | None = None
    headline: str = Field(max_length=80)
    body: str = Field(default="", max_length=300)


class SetTopicStatusOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    status_id: UUID | None = None
    headline: str | None = None
    body: str | None = None
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# Live-voice plan tools (Sprint 2 — Discord agenda authoring)
# ---------------------------------------------------------------------------


class PlanItem(BaseModel):
    """A lightweight agenda-item summary returned in plan tool outputs."""

    id: UUID
    title: str
    priority: Literal["must", "should", "optional"]
    order_hint: int


class ReadConversationPlanInput(BaseModel):
    conversation_id: UUID


class ReadConversationPlanOutput(BaseModel):
    conversation_id: UUID
    status: str
    items: list[PlanItem] = Field(default_factory=list)
    display_text: str = ""


class ListConversationPlansInput(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)


class ListConversationPlansRow(BaseModel):
    conversation_id: UUID
    status: str
    title: str  # derived from first agenda item title; fallback 'Untitled'
    item_count: int = 0
    created_at: datetime


class ListConversationPlansOutput(BaseModel):
    is_error: bool = False
    error: str | None = None
    plans: list[ListConversationPlansRow] = Field(default_factory=list)


class CreateConversationPlanInput(BaseModel):
    """Direct-write agenda from a Discord chat turn.

    No ``title`` field — the table has no title column.  The display title is
    derived from the first agenda item at render time.
    """

    plan_markdown: str = Field(
        min_length=1,
        description="Numbered (1. ...) or bulleted (- ...) list of agenda items.",
    )
    prep_summary: str | None = Field(
        default=None,
        description="Optional steering summary. Presence gates mode='steered' vs 'open'.",
    )


class CreateConversationPlanOutput(BaseModel):
    conversation_id: UUID
    status: str
    items: list[PlanItem] = Field(default_factory=list)
    display_text: str = ""


class UpdateConversationPlanInput(BaseModel):
    conversation_id: UUID
    plan_markdown: str = Field(
        min_length=1,
        description="Replacement numbered/bulleted list for the agenda.",
    )
    prep_summary: str | None = Field(
        default=None,
        description="Optional updated steering summary. Presence gates mode='steered' vs 'open'.",
    )


class UpdateConversationPlanOutput(BaseModel):
    conversation_id: UUID
    status: str
    items: list[PlanItem] = Field(default_factory=list)
    display_text: str = ""


# Single source of truth mapping tool name -> (input model, output model).
# The orchestrator uses this to validate LLM-produced tool calls and to render
# the JSON schema list passed to the Anthropic API.

TOOL_REGISTRY: dict[str, tuple[type[BaseModel], type]] = {
    "update_turn_plan": (UpdateTurnPlanInput, UpdateTurnPlanOutput),
    "submit_live_brief": (SubmitLiveBriefInput, SubmitLiveBriefOutput),
    "submit_live_debrief": (SubmitLiveDebriefInput, SubmitLiveDebriefOutput),
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
    "get_tool_call": (GetToolCallInput, GetToolCallOutput),
    "send_message_part": (SendMessagePartInput, SendMessagePartOutput),
    "consult_perspective": (ConsultPerspectiveInput, ConsultPerspectiveOutput),
    "list_bridge_candidates": (ListBridgeCandidatesInput, ListBridgeCandidatesOutput),
    "list_scheduled_tasks": (ListScheduledTasksInput, ListScheduledTasksOutput),
    # write
    "update_user_style_notes": (UpdateUserStyleNotesInput, UpdateUserStyleNotesOutput),
    "set_partner_sharing": (SetPartnerSharingInput, SetPartnerSharingOutput),
    "create_bridge_candidate": (
        CreateBridgeCandidateInput,
        CreateBridgeCandidateOutput,
    ),
    "update_bridge_candidate": (
        UpdateBridgeCandidateInput,
        UpdateBridgeCandidateOutput,
    ),
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
    "cancel_scheduled_checkin": (
        CancelScheduledCheckinInput,
        CancelScheduledCheckinOutput,
    ),
    "schedule_task": (ScheduleTaskInput, ScheduleTaskOutput),
    "update_scheduled_task": (UpdateScheduledTaskInput, UpdateScheduledTaskOutput),
    "cancel_scheduled_task": (CancelScheduledTaskInput, CancelScheduledTaskOutput),
    "update_scheduled_checkin": (UpdateScheduledCheckinInput, UpdateScheduledCheckinOutput),
    "schedule_partner_checkin": (
        SchedulePartnerCheckinInput,
        SchedulePartnerCheckinOutput,
    ),
    "cancel_partner_nudge": (CancelPartnerNudgeInput, CancelPartnerNudgeOutput),
    "list_scheduled_checkins": (
        ListScheduledCheckinsInput,
        ListScheduledCheckinsOutput,
    ),
    "list_all_reminders": (ListAllRemindersInput, ListAllRemindersOutput),
    "escalate_to_partner": (EscalateToPartnerInput, EscalateToPartnerOutput),
    "edit_outbound_message": (EditOutboundMessageInput, EditOutboundMessageOutput),
    "delete_outbound_message": (
        DeleteOutboundMessageInput,
        DeleteOutboundMessageOutput,
    ),
    "react_to_message": (ReactToMessageInput, ReactToMessageOutput),
    "explain_media_item": (ExplainMediaItemInput, ExplainMediaItemOutput),
    "log_feedback": (LogFeedbackInput, LogFeedbackOutput),
    # pregnancy
    "set_pregnancy_edd": (SetPregnancyEddInput, SetPregnancyEddOutput),
    "correct_pregnancy_edd": (CorrectPregnancyEddInput, CorrectPregnancyEddOutput),
    "end_pregnancy": (EndPregnancyInput, EndPregnancyOutput),
    # hector
    "create_commitment": (CreateCommitmentInput, CreateCommitmentOutput),
    "update_commitment": (UpdateCommitmentInput, UpdateCommitmentOutput),
    "close_commitment": (CloseCommitmentInput, CloseCommitmentOutput),
    "log_event": (LogEventInput, LogEventOutput),
    "list_commitments": (ListCommitmentsInput, ListCommitmentsOutput),
    "list_events": (ListEventsInput, ListEventsOutput),
    "get_adherence": (GetAdherenceInput, GetAdherenceOutput),
    # live-voice plan tools
    "read_conversation_plan": (ReadConversationPlanInput, ReadConversationPlanOutput),
    "list_conversation_plans": (ListConversationPlansInput, ListConversationPlansOutput),
    "create_conversation_plan": (CreateConversationPlanInput, CreateConversationPlanOutput),
    "update_conversation_plan": (UpdateConversationPlanInput, UpdateConversationPlanOutput),
}
