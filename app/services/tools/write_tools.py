"""Write tools for the agentic loop."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from app.services.checkins import schedule_checkin_record
from app.services.cross_thread_privacy import (
    raw_message_visibility,
)
from app.services.crypto import encrypt_value
from app.config import get_settings
from app.services.partner_sharing import (
    get_partner_share,
    resolve_dyad_partner,
    set_partner_share,
)
from app.services.vision import explain_stored_image
from app.services.messaging import send_outbound, _append_turn_reasoning, _call_oob_hook
from app.services.message_embedding_lifecycle import (
    enqueue_content_embed,
    enqueue_content_embedding_drop,
    enqueue_content_reembed,
    enqueue_message_embedding_drop,
    enqueue_message_reembed,
)
from app.services.embeddings import (
    canonical_distillation_embedding_text,
    canonical_memory_embedding_text,
    canonical_observation_embedding_text,
    content_hash,
)
from app.services import discord, scoring
from app.services.templates import TemplateCall
from app.services.time_context import temporal_reference
from app.services.turn_context import TurnContext, obs_fields, scope_from_turn_context
from app.services.scheduled_task_recurrence import normalize_recurrence
from app.services.tools.audit import log_tool_call as _log_tool_call_shared
from app.services.tools.common import current_scheduled_task
from app.services.tools.scope_guard import (
    check_write_scope,
    resolve_write_topic_slugs,
    require_reason_for_cross_topic,
    resolve_topic_ids,
)
from app.services.topic_filter import join_artifact_topics
from tool_schemas import (
    SetTopicStatusInput,
    SetTopicStatusOutput,
    AddDistillationInput,
    AddDistillationOutput,
    AddMemoryInput,
    AddMemoryOutput,
    AddOOBInput,
    AddOOBOutput,
    AddWatchItemInput,
    AddWatchItemOutput,
    AddressWatchItemInput,
    AddressWatchItemOutput,
    BridgeCandidate,
    BridgeCandidatePartnerPath,
    BridgeCandidateSensitivity,
    BridgeCandidateStatus,
    CancelPartnerNudgeInput,
    CancelPartnerNudgeOutput,
    CancelScheduledCheckinInput,
    CancelScheduledCheckinOutput,
    CancelScheduledTaskInput,
    CancelScheduledTaskOutput,
    CorrectPregnancyEddInput,
    CorrectPregnancyEddOutput,
    CreateBridgeCandidateInput,
    CreateBridgeCandidateOutput,
    CreateThemeInput,
    CreateThemeOutput,
    DeleteOutboundMessageInput,
    DeleteOutboundMessageOutput,
    EditOutboundMessageInput,
    EditOutboundMessageOutput,
    EndPregnancyInput,
    EndPregnancyOutput,
    EscalateToPartnerInput,
    EscalateToPartnerOutput,
    ExplainMediaItemInput,
    ExplainMediaItemOutput,
    LiftOOBInput,
    LiftOOBOutput,
    ListScheduledTasksInput,
    ListScheduledTasksOutput,
    LogFeedbackInput,
    LogFeedbackOutput,
    LogObservationInput,
    LogObservationOutput,
    LocalScheduleTime,
    ReactToMessageInput,
    ReactToMessageOutput,
    ReviseDistillationInput,
    ReviseDistillationOutput,
    ScheduleCheckinInput,
    ScheduleCheckinOutput,
    ScheduleDelay,
    SchedulePartnerCheckinInput,
    SchedulePartnerCheckinOutput,
    ScheduleTaskInput,
    ScheduleTaskOutput,
    ScheduledTaskRow,
    SendBridgeCandidateInput,
    SendBridgeCandidateOutput,
    SetPregnancyEddInput,
    SetPregnancyEddOutput,
    SupersedeMemoryInput,
    SupersedeMemoryOutput,
    UpdateBridgeCandidateInput,
    UpdateBridgeCandidateOutput,
    UpdateDistillationInput,
    UpdateDistillationOutput,
    UpdateMemoryInput,
    UpdateMemoryOutput,
    SetPartnerSharingInput,
    SetPartnerSharingOutput,
    UpdateOOBInput,
    UpdateOOBOutput,
    UpdateObservationInput,
    UpdateObservationOutput,
    UpdateScheduledCheckinInput,
    UpdateScheduledCheckinOutput,
    UpdateScheduledTaskInput,
    UpdateScheduledTaskOutput,
    UpdateThemeInput,
    UpdateThemeOutput,
    UpdateUserStyleNotesInput,
    UpdateUserStyleNotesOutput,
    UpdateWatchItemInput,
    UpdateWatchItemOutput,
    # hector
    CreateCommitmentInput,
    CreateCommitmentOutput,
    UpdateCommitmentInput,
    UpdateCommitmentOutput,
    CloseCommitmentInput,
    CloseCommitmentOutput,
    LogEventInput,
    LogEventOutput,
    # plan tools
    CreateConversationPlanInput,
    CreateConversationPlanOutput,
    PlanItem,
    UpdateConversationPlanInput,
    UpdateConversationPlanOutput,
)
from app.services.live.plan_markdown import markdown_to_agenda, agenda_to_display

logger = logging.getLogger(__name__)


def _memory_is_searchable(row: Any | None) -> bool:
    if row is None:
        return False
    return (
        row.get("status") == "active"
        and (row.get("visibility") or "private") == "private"
        and bool(canonical_memory_embedding_text(row.get("content")))
    )


def _memory_content_hash(row: Any) -> str:
    return content_hash(canonical_memory_embedding_text(row.get("content")))


async def _fetch_memory_embedding_state(pool: Any, memory_id: Any) -> Any | None:
    return await pool.fetchrow(
        """
        SELECT id, content, status, visibility
        FROM memories
        WHERE id = $1
        """,
        memory_id,
    )


async def _sync_memory_embedding_after_create(ctx: TurnContext, memory_id: Any) -> None:
    row = await _fetch_memory_embedding_state(ctx.pool, memory_id)
    if _memory_is_searchable(row):
        await enqueue_content_embed(
            ctx.pool,
            source_type="memory",
            source_id=row["id"],
            content_hash=_memory_content_hash(row),
        )


async def _sync_memory_embedding_after_update(ctx: TurnContext, memory_id: Any) -> None:
    row = await _fetch_memory_embedding_state(ctx.pool, memory_id)
    if _memory_is_searchable(row):
        await enqueue_content_reembed(
            ctx.pool,
            source_type="memory",
            source_id=row["id"],
            content_hash=_memory_content_hash(row),
        )
    else:
        await enqueue_content_embedding_drop(
            ctx.pool,
            source_type="memory",
            source_id=memory_id,
        )


async def _sync_memory_embedding_after_supersede(
    ctx: TurnContext, *, old_memory_id: Any, new_memory_id: Any
) -> None:
    await enqueue_content_embedding_drop(
        ctx.pool,
        source_type="memory",
        source_id=old_memory_id,
    )
    await _sync_memory_embedding_after_create(ctx, new_memory_id)


def _observation_is_searchable(row: Any | None) -> bool:
    if row is None:
        return False
    return (
        row.get("status") == "active"
        and (row.get("significance") or 0) >= 3
        and bool(canonical_observation_embedding_text(row.get("content")))
    )


def _observation_content_hash(row: Any) -> str:
    return content_hash(canonical_observation_embedding_text(row.get("content")))


async def _fetch_observation_embedding_state(pool: Any, observation_id: Any) -> Any | None:
    return await pool.fetchrow(
        """
        SELECT id, content, status, significance
        FROM observations
        WHERE id = $1
        """,
        observation_id,
    )


async def _sync_observation_embedding_after_create(
    ctx: TurnContext, observation_id: Any
) -> None:
    row = await _fetch_observation_embedding_state(ctx.pool, observation_id)
    if _observation_is_searchable(row):
        await enqueue_content_embed(
            ctx.pool,
            source_type="observation",
            source_id=row["id"],
            content_hash=_observation_content_hash(row),
        )


async def _sync_observation_embedding_after_update(
    ctx: TurnContext, observation_id: Any
) -> None:
    row = await _fetch_observation_embedding_state(ctx.pool, observation_id)
    if _observation_is_searchable(row):
        await enqueue_content_reembed(
            ctx.pool,
            source_type="observation",
            source_id=row["id"],
            content_hash=_observation_content_hash(row),
        )
    else:
        await enqueue_content_embedding_drop(
            ctx.pool,
            source_type="observation",
            source_id=observation_id,
        )


def _distillation_is_searchable(row: Any | None) -> bool:
    if row is None:
        return False
    return (
        row.get("status") == "active"
        and (row.get("visibility") or "private") == "private"
        and row.get("superseded_by_distillation_id") is None
        and row.get("retired_at") is None
        and row.get("revised_at") is None
        and bool(canonical_distillation_embedding_text(row.get("content")))
    )


def _distillation_content_hash(row: Any) -> str:
    return content_hash(canonical_distillation_embedding_text(row.get("content")))


async def _fetch_distillation_embedding_state(
    pool: Any, distillation_id: Any
) -> Any | None:
    return await pool.fetchrow(
        """
        SELECT
            id, content, status, visibility, superseded_by_distillation_id,
            revised_at, retired_at
        FROM distillations
        WHERE id = $1
        """,
        distillation_id,
    )


async def _sync_distillation_embedding_after_create(
    ctx: TurnContext, distillation_id: Any
) -> None:
    row = await _fetch_distillation_embedding_state(ctx.pool, distillation_id)
    if _distillation_is_searchable(row):
        await enqueue_content_embed(
            ctx.pool,
            source_type="distillation",
            source_id=row["id"],
            content_hash=_distillation_content_hash(row),
        )


async def _sync_distillation_embedding_after_update(
    ctx: TurnContext, distillation_id: Any
) -> None:
    row = await _fetch_distillation_embedding_state(ctx.pool, distillation_id)
    if _distillation_is_searchable(row):
        await enqueue_content_reembed(
            ctx.pool,
            source_type="distillation",
            source_id=row["id"],
            content_hash=_distillation_content_hash(row),
        )
    else:
        await enqueue_content_embedding_drop(
            ctx.pool,
            source_type="distillation",
            source_id=distillation_id,
        )


async def _sync_distillation_embedding_after_revise(
    ctx: TurnContext, *, old_distillation_id: Any, new_distillation_id: Any
) -> None:
    await enqueue_content_embedding_drop(
        ctx.pool,
        source_type="distillation",
        source_id=old_distillation_id,
    )
    await _sync_distillation_embedding_after_create(ctx, new_distillation_id)

SCORING_PROMPT_VERSION = scoring.SCORING_PROMPT_VERSION
_BRIDGE_RESOLVED_STATUSES = {"sent", "declined", "blocked", "addressed", "expired"}


class ToolCallRejected(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("error", "tool call rejected"))
        self.result = result


def _json_payload(value: BaseModel | dict[str, Any]) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, default=str)


def _jsonb_payload(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return json.loads(value.model_dump_json())
    return value


async def _log_tool_call(
    ctx: TurnContext,
    name: str,
    args: BaseModel,
    started_at: datetime,
    result: BaseModel | dict[str, Any],
) -> None:
    """Back-compat shim: delegate to the shared audit logger as a write."""
    await _log_tool_call_shared(
        ctx, name, args, started_at, result, kind="write"
    )


def _start() -> datetime:
    return datetime.now(UTC)


def _message_thread_owner_id(row: Any) -> Any:
    if row["direction"] == "inbound" and row["sender_id"] is not None:
        return row["sender_id"]
    if row["direction"] == "outbound" and row["recipient_id"] is not None:
        return row["recipient_id"]
    return row["sender_id"] or row["recipient_id"]


async def _schedule_context_job(
    pool: Any,
    *,
    user_id: Any,
    job_type: str,
    scheduled_for: datetime,
    context_key: str,
    context_id: Any,
    bot_id: str,
    topic_id: Any = None,
) -> None:
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET status='superseded'
        WHERE job_type=$1
          AND status='pending'
          AND context->>$2 = $3
        """,
        job_type,
        context_key,
        str(context_id),
    )
    await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
        VALUES ($1, $2, $3, $4::jsonb, 'pending', $5, $6)
        RETURNING id, scheduled_for
        """,
        user_id,
        job_type,
        scheduled_for,
        {context_key: str(context_id)},
        bot_id,
        topic_id,
    )


async def update_user_style_notes(
    ctx: TurnContext, args: UpdateUserStyleNotesInput
) -> UpdateUserStyleNotesOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    row = await ctx.pool.fetchrow(
        "UPDATE users SET style_notes=$1 WHERE id=$2 RETURNING id AS user_id, now() AS updated_at",
        args.notes,
        args.user_id,
    )
    result = UpdateUserStyleNotesOutput(
        user_id=row["user_id"], updated_at=row["updated_at"]
    )
    await _log_tool_call(ctx, "update_user_style_notes", args, started, result)
    return result


async def set_partner_sharing(
    ctx: TurnContext,
    args: SetPartnerSharingInput,
) -> SetPartnerSharingOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    if ctx.bot_id is None:
        result = {
            "error": "partner_sharing_rejected",
            "reason": "set_partner_sharing requires the calling bot scope",
        }
        await _log_tool_call(ctx, "set_partner_sharing", args, started, result)
        raise ToolCallRejected(result)
    partner_share = await set_partner_share(
        ctx.pool,
        user_id=ctx.user.id,
        bot_id=ctx.bot_id,
        opt_in=args.opt_in,
    )
    updated_at = datetime.now(UTC)
    result = SetPartnerSharingOutput(
        user_id=ctx.user.id,
        bot_id=ctx.bot_id,
        partner_share=partner_share,
        updated_at=updated_at,
    )
    await _append_turn_reasoning(
        ctx.pool,
        ctx.turn_id,
        f"Partner sharing set for user_id={ctx.user.id}, bot_id={ctx.bot_id}: {partner_share}. reason={args.reason}",
    )
    await _log_tool_call(ctx, "set_partner_sharing", args, started, result)
    return result


async def create_bridge_candidate(
    ctx: TurnContext,
    args: CreateBridgeCandidateInput,
) -> CreateBridgeCandidateOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    if args.partner_path == BridgeCandidatePartnerPath.do_not_bridge:
        if args.status is not None and args.status != BridgeCandidateStatus.declined:
            result = {
                "error": "bridge_candidate_status_rejected",
                "reason": "do_not_bridge candidates are audit-only and must be created as declined",
            }
            await _log_tool_call(ctx, "create_bridge_candidate", args, started, result)
            raise ToolCallRejected(result)
    elif args.status == BridgeCandidateStatus.declined:
        result = {
            "error": "bridge_candidate_status_rejected",
            "reason": "declined status is only accepted when partner_path is do_not_bridge",
        }
        await _log_tool_call(ctx, "create_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    if args.status is not None and args.status != BridgeCandidateStatus.blocked:
        if not (
            args.partner_path == BridgeCandidatePartnerPath.do_not_bridge
            and args.status == BridgeCandidateStatus.declined
        ):
            result = {
                "error": "bridge_candidate_status_rejected",
                "reason": "create_bridge_candidate only accepts explicit blocked status; otherwise status is derived",
            }
            await _log_tool_call(ctx, "create_bridge_candidate", args, started, result)
            raise ToolCallRejected(result)
    if args.source_user_id != ctx.user.id:
        result = {
            "error": "bridge_candidate_rejected",
            "reason": "bridge candidates must be created from the current user's thread as source",
        }
        await _log_tool_call(ctx, "create_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    if not _bridge_users_in_current_dyad(ctx, args.source_user_id, args.target_user_id):
        result = {
            "error": "bridge_candidate_rejected",
            "reason": "bridge candidates must stay within the current dyad and source must differ from target",
        }
        await _log_tool_call(ctx, "create_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    await _require_existing_source_messages(
        ctx, args.source_message_ids, args.source_user_id
    )
    await _require_existing_ids(ctx, "memories", args.related_memory_ids)
    await _require_existing_ids(ctx, "observations", args.related_observation_ids)

    if args.partner_path == BridgeCandidatePartnerPath.do_not_bridge:
        status = BridgeCandidateStatus.declined
    elif args.status == BridgeCandidateStatus.blocked:
        status = BridgeCandidateStatus.blocked
    else:
        source_partner_share = await _partner_share_for_bot(ctx, args.source_user_id)
        low_or_medium = args.sensitivity in {
            BridgeCandidateSensitivity.low,
            BridgeCandidateSensitivity.medium,
        }
        status = (
            BridgeCandidateStatus.ready
            if source_partner_share == "opt_in" and low_or_medium
            else BridgeCandidateStatus.pending
        )
    resolved_at_sql = "now()" if status.value in _BRIDGE_RESOLVED_STATUSES else "NULL"
    row = await ctx.pool.fetchrow(
        f"""
        INSERT INTO bridge_candidates (
            source_user_id, target_user_id, kind, status, sensitivity, partner_path,
            source_message_ids, related_memory_ids, related_observation_ids,
            internal_note, shareable_summary, resolved_at,
            bot_id, topic_id, dyad_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8::uuid[], $9::uuid[], $10, $11, {resolved_at_sql}, $12, $13, $14)
        RETURNING id, source_user_id, target_user_id, kind, status, sensitivity, partner_path,
                  COALESCE(source_message_ids, '{{}}'::uuid[]) AS source_message_ids,
                  COALESCE(related_memory_ids, '{{}}'::uuid[]) AS related_memory_ids,
                  COALESCE(related_observation_ids, '{{}}'::uuid[]) AS related_observation_ids,
                  internal_note, shareable_summary, sent_message_id,
                  created_at, updated_at, resolved_at
        """,
        args.source_user_id,
        args.target_user_id,
        args.kind.value,
        status.value,
        args.sensitivity.value,
        args.partner_path.value,
        args.source_message_ids,
        args.related_memory_ids,
        args.related_observation_ids,
        args.internal_note or "",
        args.shareable_summary,
        ctx.bot_id,
        ctx.primary_topic_id,
        ctx.dyad_id,
    )
    result = CreateBridgeCandidateOutput(candidate=_bridge_candidate(row))
    await _log_tool_call(ctx, "create_bridge_candidate", args, started, result)
    return result


async def update_bridge_candidate(
    ctx: TurnContext,
    args: UpdateBridgeCandidateInput,
) -> UpdateBridgeCandidateOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    existing = await _fetch_bridge_candidate_row(ctx, args.candidate_id)
    if existing is None:
        result = {
            "error": "bridge_candidate_not_found",
            "reason": "candidate is not in the current dyad",
        }
        await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    if existing["source_user_id"] != ctx.user.id:
        target_allowed_statuses = {
            BridgeCandidateStatus.addressed,
            BridgeCandidateStatus.declined,
        }
        if (
            existing["status"] not in {"ready", "sent", "addressed"}
            or args.kind is not None
            or args.sensitivity is not None
            or args.source_message_ids is not None
            or args.related_memory_ids is not None
            or args.related_observation_ids is not None
            or args.internal_note is not None
            or args.shareable_summary is not None
            or args.partner_path is not None
            or args.status not in target_allowed_statuses
        ):
            result = {
                "error": "bridge_candidate_update_rejected",
                "reason": "target-side bridge updates can only mark a visible candidate addressed or declined",
            }
            await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
            raise ToolCallRejected(result)
    status_update = args.status
    if (
        args.partner_path is not None
        and existing["status"] in _BRIDGE_RESOLVED_STATUSES
    ):
        result = {
            "error": "bridge_candidate_partner_path_locked",
            "reason": "partner_path cannot be changed after a bridge candidate reaches a terminal status",
        }
        await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    if args.partner_path == BridgeCandidatePartnerPath.do_not_bridge:
        if (
            status_update is not None
            and status_update != BridgeCandidateStatus.declined
        ):
            result = {
                "error": "bridge_candidate_status_rejected",
                "reason": "do_not_bridge candidates must be marked declined",
            }
            await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
            raise ToolCallRejected(result)
        status_update = BridgeCandidateStatus.declined
    if args.source_message_ids is not None:
        await _require_existing_source_messages(
            ctx, args.source_message_ids, existing["source_user_id"]
        )
    if args.related_memory_ids is not None:
        await _require_existing_ids(ctx, "memories", args.related_memory_ids)
    if args.related_observation_ids is not None:
        await _require_existing_ids(ctx, "observations", args.related_observation_ids)
    row = await ctx.pool.fetchrow(
        """
        UPDATE bridge_candidates
        SET kind=COALESCE($2::text, kind),
            status=COALESCE($3::text, status),
            sensitivity=COALESCE($4::text, sensitivity),
            partner_path=COALESCE($5::text, partner_path),
            source_message_ids=COALESCE($6::uuid[], source_message_ids),
            related_memory_ids=COALESCE($7::uuid[], related_memory_ids),
            related_observation_ids=COALESCE($8::uuid[], related_observation_ids),
            internal_note=COALESCE($9::text, internal_note),
            shareable_summary=COALESCE($10::text, shareable_summary),
            updated_at=now(),
            resolved_at=CASE
                WHEN $3::text IN ('sent', 'declined', 'blocked', 'addressed', 'expired')
                    THEN COALESCE(resolved_at, now())
                ELSE resolved_at
            END
        WHERE id=$1
        RETURNING id, source_user_id, target_user_id, kind, status, sensitivity, partner_path,
                  COALESCE(source_message_ids, '{}'::uuid[]) AS source_message_ids,
                  COALESCE(related_memory_ids, '{}'::uuid[]) AS related_memory_ids,
                  COALESCE(related_observation_ids, '{}'::uuid[]) AS related_observation_ids,
                  internal_note, shareable_summary, sent_message_id,
                  created_at, updated_at, resolved_at
        """,
        args.candidate_id,
        args.kind.value if args.kind is not None else None,
        status_update.value if status_update is not None else None,
        args.sensitivity.value if args.sensitivity is not None else None,
        args.partner_path.value if args.partner_path is not None else None,
        args.source_message_ids,
        args.related_memory_ids,
        args.related_observation_ids,
        args.internal_note,
        args.shareable_summary,
    )
    result = UpdateBridgeCandidateOutput(
        candidate=_bridge_candidate_for_context(ctx, row)
    )
    await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
    return result


async def send_bridge_candidate(
    ctx: TurnContext,
    args: SendBridgeCandidateInput,
) -> SendBridgeCandidateOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    existing = await _fetch_bridge_candidate_row(ctx, args.candidate_id)
    if existing is None or existing["source_user_id"] != ctx.user.id:
        result = {
            "error": "bridge_candidate_not_found",
            "reason": "candidate must exist in this dyad with the current user as source",
        }
        await _log_tool_call(ctx, "send_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    if existing["status"] != BridgeCandidateStatus.ready.value:
        result = {
            "error": "bridge_candidate_not_ready",
            "reason": "send_bridge_candidate only sends candidates in ready status",
        }
        await _log_tool_call(ctx, "send_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    target = ctx.partner if existing["target_user_id"] == ctx.partner.id else None
    if target is None:
        result = {
            "error": "bridge_candidate_target_rejected",
            "reason": "ready candidate target must be the current partner",
        }
        await _log_tool_call(ctx, "send_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)

    content = existing["shareable_summary"]
    protected_owner_ids = [existing["source_user_id"], existing["target_user_id"]]
    turn_scope = scope_from_turn_context(ctx)
    verdict = await _call_oob_hook(
        ctx.pool,
        content,
        target.id,
        protected_owner_ids,
        bot_id=turn_scope.bot_id,
        topic_id=turn_scope.topic_id,
    )
    if verdict["verdict"] in {"block", "rewrite"}:
        note = _append_note(
            existing["internal_note"],
            f"OOB {verdict['verdict']}: {verdict.get('reason', '')}",
        )
        row = await _set_bridge_candidate_status(
            ctx,
            args.candidate_id,
            BridgeCandidateStatus.blocked,
            internal_note=note,
        )
        result = SendBridgeCandidateOutput(candidate=_bridge_candidate(row))
        await _log_tool_call(ctx, "send_bridge_candidate", args, started, result)
        return result

    sent_message_id = await send_outbound(
        ctx.pool,
        target,
        content,
        bot_turn_id=ctx.turn_id,
        protected_owner_ids=protected_owner_ids,
        scope=turn_scope,
    )
    row = await _set_bridge_candidate_status(
        ctx,
        args.candidate_id,
        BridgeCandidateStatus.sent,
        sent_message_id=sent_message_id,
    )
    await _append_turn_reasoning(
        ctx.pool,
        ctx.turn_id,
        f"Bridge candidate sent candidate_id={args.candidate_id} sent_message_id={sent_message_id}. reason={args.reason or ''}",
    )
    result = SendBridgeCandidateOutput(candidate=_bridge_candidate(row))
    await _log_tool_call(ctx, "send_bridge_candidate", args, started, result)
    return result


def _bridge_users_in_current_dyad(
    ctx: TurnContext, source_user_id: Any, target_user_id: Any
) -> bool:
    dyad = {ctx.user.id, ctx.partner.id}
    return (
        source_user_id in dyad
        and target_user_id in dyad
        and source_user_id != target_user_id
    )


async def _partner_share_for_bot(
    ctx: TurnContext, user_id: Any, bot_id: str | None = None
) -> str:
    effective_bot_id = bot_id if bot_id is not None else ctx.bot_id
    if effective_bot_id is None:
        return "unset"
    partner_share = await get_partner_share(
        ctx.pool, user_id=user_id, bot_id=effective_bot_id
    )
    return partner_share or "unset"


async def _require_existing_source_messages(
    ctx: TurnContext, message_ids: list[Any], source_user_id: Any
) -> None:
    rows = await ctx.pool.fetch(
        """
        SELECT id
        FROM messages
        WHERE id = ANY($1::uuid[])
          AND deleted_at IS NULL
          AND (sender_id=$2 OR recipient_id=$2)
          AND bot_id=$3
          AND topic_id=$4
        """,
        message_ids,
        source_user_id,
        ctx.bot_id,
        ctx.primary_topic_id,
    )
    found = {row["id"] for row in rows}
    missing = [message_id for message_id in message_ids if message_id not in found]
    if missing:
        raise ToolCallRejected(
            {
                "error": "bridge_source_messages_not_found",
                "reason": f"source_message_ids are not visible source-thread messages: {missing}",
            }
        )


async def _require_existing_ids(ctx: TurnContext, table: str, ids: list[Any]) -> None:
    if not ids:
        return
    if table not in {"memories", "observations"}:
        raise ValueError("unsupported bridge link table")
    rows = await ctx.pool.fetch(
        f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", ids
    )
    found = {row["id"] for row in rows}
    missing = [row_id for row_id in ids if row_id not in found]
    if missing:
        raise ToolCallRejected(
            {
                "error": "bridge_related_ids_not_found",
                "reason": f"{table} ids were not found: {missing}",
            }
        )


async def _require_existing_distillation_links(
    ctx: TurnContext,
    *,
    source_user_ids: list[Any] | None = None,
    related_memory_ids: list[Any] | None = None,
    related_observation_ids: list[Any] | None = None,
    related_theme_ids: list[Any] | None = None,
    supporting_message_ids: list[Any] | None = None,
) -> None:
    dyad_ids = {ctx.user.id, ctx.partner.id}
    if source_user_ids is not None:
        invalid_sources = [
            user_id for user_id in source_user_ids if user_id not in dyad_ids
        ]
        if invalid_sources:
            raise ToolCallRejected(
                {
                    "error": "distillation_source_users_rejected",
                    "reason": f"source_user_ids must stay within the current dyad: {invalid_sources}",
                }
            )
    for table, ids in (
        ("memories", related_memory_ids or []),
        ("observations", related_observation_ids or []),
        ("themes", related_theme_ids or []),
        ("messages", supporting_message_ids or []),
    ):
        if not ids:
            continue
        if table == "messages":
            rows = await ctx.pool.fetch(
                """
                SELECT id
                FROM messages
                WHERE id = ANY($1::uuid[])
                  AND deleted_at IS NULL
                  AND bot_id=$3
                  AND topic_id=$4
                  AND (
                    sender_id = ANY($2::uuid[])
                    OR recipient_id = ANY($2::uuid[])
                  )
                """,
                ids,
                list(dyad_ids),
                ctx.bot_id,
                ctx.primary_topic_id,
            )
        else:
            alias = {"memories": "m", "observations": "o", "themes": "t"}.get(
                table, table[:1]
            )
            rows = await ctx.pool.fetch(
                f"SELECT {alias}.id FROM {table} {alias} {join_artifact_topics(alias, '$2')} WHERE {alias}.id = ANY($1::uuid[])",
                ids,
                ctx.primary_topic_id,
            )
        found = {row["id"] for row in rows}
        missing = [row_id for row_id in ids if row_id not in found]
        if missing:
            raise ToolCallRejected(
                {
                    "error": "distillation_related_ids_not_found",
                    "reason": f"{table} ids were not found or not visible: {missing}",
                }
            )


def _default_supporting_message_ids(
    ctx: TurnContext, message_ids: list[Any]
) -> list[Any]:
    return message_ids or list(ctx.triggering_message_ids)


async def _fetch_bridge_candidate_row(
    ctx: TurnContext, candidate_id: Any
) -> Any | None:
    return await ctx.pool.fetchrow(
        """
        SELECT id, source_user_id, target_user_id, kind, status, sensitivity, partner_path,
               COALESCE(source_message_ids, '{}'::uuid[]) AS source_message_ids,
               COALESCE(related_memory_ids, '{}'::uuid[]) AS related_memory_ids,
               COALESCE(related_observation_ids, '{}'::uuid[]) AS related_observation_ids,
               internal_note, shareable_summary, sent_message_id,
               created_at, updated_at, resolved_at
        FROM bridge_candidates
        WHERE id=$1
          AND (
            (source_user_id=$2 AND target_user_id=$3)
            OR (source_user_id=$3 AND target_user_id=$2)
        )
        """,
        candidate_id,
        ctx.user.id,
        ctx.partner.id,
    )


async def _set_bridge_candidate_status(
    ctx: TurnContext,
    candidate_id: Any,
    status: BridgeCandidateStatus,
    *,
    sent_message_id: Any | None = None,
    internal_note: str | None = None,
) -> Any:
    return await ctx.pool.fetchrow(
        """
        UPDATE bridge_candidates
        SET status=$2,
            sent_message_id=COALESCE($3::uuid, sent_message_id),
            internal_note=COALESCE($4::text, internal_note),
            updated_at=now(),
            resolved_at=CASE
                WHEN $2::text IN ('sent', 'declined', 'blocked', 'addressed', 'expired')
                    THEN COALESCE(resolved_at, now())
                ELSE resolved_at
            END
        WHERE id=$1
        RETURNING id, source_user_id, target_user_id, kind, status, sensitivity, partner_path,
                  COALESCE(source_message_ids, '{}'::uuid[]) AS source_message_ids,
                  COALESCE(related_memory_ids, '{}'::uuid[]) AS related_memory_ids,
                  COALESCE(related_observation_ids, '{}'::uuid[]) AS related_observation_ids,
                  internal_note, shareable_summary, sent_message_id,
                  created_at, updated_at, resolved_at
        """,
        candidate_id,
        status.value,
        sent_message_id,
        internal_note,
    )


def _append_note(existing: str | None, note: str) -> str:
    return f"{existing}\n{note}" if existing else note


def _assert_solo_about_user(
    ctx: TurnContext,
    target_user_id: Any,
    *,
    field_name: str = "about_user_id",
) -> dict[str, Any] | None:
    """Validate that *target_user_id* is ctx.user.id for solo bots.

    When bot_spec.participants_shape == 'solo', reject:
      - target_user_id is None (must always target the bound user)
      - target_user_id != ctx.user.id (cannot target anyone else)

    *field_name* tunes the error message so guards on owner_user_id /
    owner_id read sensibly.  Returns a tool-error dict (matching _tool_error
    in registry.py:306) on failure, or None if the check passes.  This runs
    BEFORE the scope guard per lesson #8 — it is an additive pre-check, not
    a replacement.
    """
    if (
        getattr(ctx, "bot_spec", None) is None
        or ctx.bot_spec.participants_shape != "solo"
    ):
        return None
    if target_user_id is None:
        return {"error": f"{field_name} is required for solo bots", "is_error": True}
    if target_user_id != ctx.user.id:
        return {
            "error": f"{field_name} {target_user_id} does not match the solo bot's bound user {ctx.user.id}",
            "is_error": True,
        }
    return None


async def _assert_solo_owns_row(
    ctx: TurnContext,
    *,
    table: str,
    row_id: Any,
    owner_field: str,
) -> dict[str, Any] | None:
    """For solo bots, ensure that the existing row at *table.row_id* is owned by ctx.user.id.

    Returns a tool-error dict if the row does not exist or the owner field
    does not match the bound user; None otherwise.  No-op for non-solo bots.

    The SELECT is unconditional (no topic join) — scope guards still apply
    separately, but ownership is a stronger invariant: even within scope, a
    solo bot must not mutate another user's row.
    """
    if (
        getattr(ctx, "bot_spec", None) is None
        or ctx.bot_spec.participants_shape != "solo"
    ):
        return None
    actual = await ctx.pool.fetchval(
        f"SELECT {owner_field} FROM {table} WHERE id = $1",
        row_id,
    )
    if actual is None:
        return {
            "error": f"{table} row {row_id} not found",
            "is_error": True,
        }
    if actual != ctx.user.id:
        return {
            "error": (
                f"{table}.{owner_field} {actual} does not match the solo bot's bound user "
                f"{ctx.user.id}"
            ),
            "is_error": True,
        }
    return None


def _bridge_candidate(row: Any) -> BridgeCandidate:
    data = dict(row)
    data.setdefault("partner_path", "message_partner")
    data["source_message_ids"] = list(data.get("source_message_ids") or [])
    data["related_memory_ids"] = list(data.get("related_memory_ids") or [])
    data["related_observation_ids"] = list(data.get("related_observation_ids") or [])
    return BridgeCandidate.model_validate(data)


def _bridge_candidate_for_context(ctx: TurnContext, row: Any) -> BridgeCandidate:
    data = dict(row)
    if data["target_user_id"] == ctx.user.id and data["source_user_id"] != ctx.user.id:
        data["internal_note"] = None
    return _bridge_candidate(data)


async def add_memory(ctx: TurnContext, args: AddMemoryInput) -> AddMemoryOutput:
    _solo_err = _assert_solo_about_user(ctx, args.about_user_id)
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    topic_slugs = resolve_write_topic_slugs(ctx, args.topic_slugs)
    require_reason_for_cross_topic(topic_slugs, ctx.primary_topic_slug, args.reason)
    topic_id_map = await resolve_topic_ids(ctx.pool, topic_slugs)
    topic_id_list = [topic_id_map[slug] for slug in topic_slugs]
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        WITH new_artifact AS (
            INSERT INTO memories (
                about_user_id, content, content_encrypted, visibility,
                shareable_summary, shareable_summary_encrypted,
                related_theme_ids, recorded_by_bot_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        )
        INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
        SELECT 'memories', new_artifact.id, t.tid, $8, 'active', $10
        FROM new_artifact CROSS JOIN unnest($9::uuid[]) AS t(tid)
        RETURNING artifact_id AS id
        """,
        args.about_user_id,
        args.content,
        encrypt_value(args.content),
        args.visibility.value,
        args.shareable_summary,
        (
            encrypt_value(args.shareable_summary)
            if args.shareable_summary is not None
            else None
        ),
        args.related_theme_ids,
        ctx.bot_id,
        topic_id_list,
        args.reason,
    )
    result = AddMemoryOutput(id=row["id"])
    await _sync_memory_embedding_after_create(ctx, row["id"])
    await _log_tool_call(ctx, "add_memory", args, started, result)
    return result


async def update_memory(
    ctx: TurnContext, args: UpdateMemoryInput
) -> UpdateMemoryOutput:
    _solo_err = await _assert_solo_owns_row(
        ctx, table="memories", row_id=args.memory_id, owner_field="about_user_id"
    )
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    sets: list[str] = []
    params: list[Any] = []
    if args.content is not None:
        params.append(args.content)
        sets.append(f"content=${len(params)}")
        params.append(encrypt_value(args.content))
        sets.append(f"content_encrypted=${len(params)}")
    if args.related_theme_ids is not None:
        params.append(args.related_theme_ids)
        sets.append(f"related_theme_ids=${len(params)}")
    if args.status is not None:
        params.append(args.status.value)
        sets.append(f"status=${len(params)}")
    if not sets:
        sets.append("last_referenced_at=now()")
    params.append(args.memory_id)
    row = await ctx.pool.fetchrow(
        f"UPDATE memories SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id",
        *params,
    )
    result = UpdateMemoryOutput(id=row["id"])
    await _sync_memory_embedding_after_update(ctx, row["id"])
    await _log_tool_call(ctx, "update_memory", args, started, result)
    return result


async def supersede_memory(
    ctx: TurnContext, args: SupersedeMemoryInput
) -> SupersedeMemoryOutput:
    _solo_err = await _assert_solo_owns_row(
        ctx, table="memories", row_id=args.old_memory_id, owner_field="about_user_id"
    )
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        WITH old AS (
            UPDATE memories SET status='superseded'
            WHERE id=$1
            RETURNING id, about_user_id
        ),
        new AS (
            INSERT INTO memories (about_user_id, content, content_encrypted, related_theme_ids, supersedes_memory_id, recorded_by_bot_id)
            SELECT about_user_id, $2, $3, $4, id, $5 FROM old
            RETURNING id AS new_id, $1::uuid AS old_id
        ),
        topic_link AS (
            INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status)
            SELECT 'memories', new.new_id, $6, $5, 'active'
            FROM new
        )
        SELECT new_id, old_id FROM new
        """,
        args.old_memory_id,
        args.new_content,
        encrypt_value(args.new_content),
        args.related_theme_ids,
        ctx.bot_id,
        ctx.primary_topic_id,
    )
    result = SupersedeMemoryOutput(new_id=row["new_id"], old_id=row["old_id"])
    await _sync_memory_embedding_after_supersede(
        ctx,
        old_memory_id=row["old_id"],
        new_memory_id=row["new_id"],
    )
    await _log_tool_call(ctx, "supersede_memory", args, started, result)
    return result


async def create_theme(ctx: TurnContext, args: CreateThemeInput) -> CreateThemeOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    topic_slugs = resolve_write_topic_slugs(ctx, args.topic_slugs)
    require_reason_for_cross_topic(topic_slugs, ctx.primary_topic_slug, args.reason)
    topic_id_map = await resolve_topic_ids(ctx.pool, topic_slugs)
    topic_id_list = [topic_id_map[slug] for slug in topic_slugs]
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        WITH new_artifact AS (
            INSERT INTO themes (title, description, sentiment, health, last_reinforced_at, recorded_by_bot_id)
            VALUES ($1, $2, $3, $4, now(), $5)
            RETURNING id
        )
        INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
        SELECT 'themes', new_artifact.id, t.tid, $5, 'active', $7
        FROM new_artifact CROSS JOIN unnest($6::uuid[]) AS t(tid)
        RETURNING artifact_id AS id
        """,
        args.title,
        args.description,
        args.sentiment.value,
        args.health.value,
        ctx.bot_id,
        topic_id_list,
        args.reason,
    )
    result = CreateThemeOutput(id=row["id"])
    await _log_tool_call(ctx, "create_theme", args, started, result)
    return result


async def update_theme(ctx: TurnContext, args: UpdateThemeInput) -> UpdateThemeOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    sets = ["updated_at=now()"]
    params: list[Any] = []
    for field in ("title", "description", "status", "sentiment", "health"):
        value = getattr(args, field)
        if value is not None:
            params.append(value.value if hasattr(value, "value") else value)
            sets.append(f"{field}=${len(params)}")
    if args.mark_reinforced:
        sets.append("last_reinforced_at=now()")
    params.append(args.theme_id)
    row = await ctx.pool.fetchrow(
        f"UPDATE themes SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id",
        *params,
    )
    result = UpdateThemeOutput(id=row["id"])
    await _log_tool_call(ctx, "update_theme", args, started, result)
    return result


async def add_watch_item(
    ctx: TurnContext, args: AddWatchItemInput
) -> AddWatchItemOutput:
    _solo_err = _assert_solo_about_user(
        ctx, args.owner_user_id, field_name="owner_user_id"
    )
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    topic_slugs = resolve_write_topic_slugs(ctx, args.topic_slugs)
    require_reason_for_cross_topic(topic_slugs, ctx.primary_topic_slug, args.reason)
    topic_id_map = await resolve_topic_ids(ctx.pool, topic_slugs)
    topic_id_list = [topic_id_map[slug] for slug in topic_slugs]
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        WITH new_artifact AS (
            INSERT INTO watch_items (owner_user_id, content, due_at, related_theme_ids, recorded_by_bot_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        )
        INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
        SELECT 'watch_items', new_artifact.id, t.tid, $5, 'active', $7
        FROM new_artifact CROSS JOIN unnest($6::uuid[]) AS t(tid)
        RETURNING artifact_id AS id
        """,
        args.owner_user_id,
        args.content,
        args.due_at,
        args.related_theme_ids,
        ctx.bot_id,
        topic_id_list,
        args.reason,
    )
    if args.due_at is not None:
        await _schedule_context_job(
            ctx.pool,
            user_id=args.owner_user_id,
            job_type="watch_item_due",
            scheduled_for=args.due_at,
            context_key="watch_item_id",
            context_id=row["id"],
            bot_id=ctx.bot_id,
            topic_id=ctx.primary_topic_id,
        )
    result = AddWatchItemOutput(id=row["id"])
    await _log_tool_call(ctx, "add_watch_item", args, started, result)
    return result


async def update_watch_item(
    ctx: TurnContext, args: UpdateWatchItemInput
) -> UpdateWatchItemOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    sets: list[str] = []
    params: list[Any] = []
    for field in ("content", "due_at", "related_theme_ids"):
        value = getattr(args, field)
        if value is not None:
            params.append(value)
            sets.append(f"{field}=${len(params)}")
    if not sets:
        sets.append("content=content")
    params.append(args.watch_item_id)
    row = await ctx.pool.fetchrow(
        f"UPDATE watch_items SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id",
        *params,
    )
    if args.due_at is not None:
        owner_user_id = await ctx.pool.fetchval(
            f"SELECT w.owner_user_id FROM watch_items w {join_artifact_topics('w', '$2')} WHERE w.id=$1",
            args.watch_item_id,
            ctx.primary_topic_id,
        )
        await _schedule_context_job(
            ctx.pool,
            user_id=owner_user_id,
            job_type="watch_item_due",
            scheduled_for=args.due_at,
            context_key="watch_item_id",
            context_id=args.watch_item_id,
            bot_id=ctx.bot_id,
            topic_id=ctx.primary_topic_id,
        )
    result = UpdateWatchItemOutput(id=row["id"])
    await _log_tool_call(ctx, "update_watch_item", args, started, result)
    return result


async def address_watch_item(
    ctx: TurnContext, args: AddressWatchItemInput
) -> AddressWatchItemOutput:
    _solo_err = await _assert_solo_owns_row(
        ctx, table="watch_items", row_id=args.watch_item_id, owner_field="owner_user_id"
    )
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        UPDATE watch_items
        SET status='addressed', addressing_note=$1, addressed_at=now()
        WHERE id=$2
        RETURNING id, addressed_at
        """,
        args.addressing_note,
        args.watch_item_id,
    )
    result = AddressWatchItemOutput(id=row["id"], addressed_at=row["addressed_at"])
    await _log_tool_call(ctx, "address_watch_item", args, started, result)
    return result


async def log_observation(
    ctx: TurnContext, args: LogObservationInput
) -> LogObservationOutput:
    _solo_err = _assert_solo_about_user(ctx, args.about_user_id)
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    topic_slugs = resolve_write_topic_slugs(ctx, args.topic_slugs)
    require_reason_for_cross_topic(topic_slugs, ctx.primary_topic_slug, args.reason)
    topic_id_map = await resolve_topic_ids(ctx.pool, topic_slugs)
    topic_id_list = [topic_id_map[slug] for slug in topic_slugs]
    started = _start()
    significance = args.significance
    supporting_message_ids = args.supporting_message_ids or ctx.triggering_message_ids
    logged_args = args.model_copy(
        update={"supporting_message_ids": supporting_message_ids}
    )
    scoring_prompt_version = SCORING_PROMPT_VERSION
    if significance is None:
        significance, _reason, scoring_prompt_version = await scoring.score_observation(
            ctx.pool, content=args.content
        )
    row = await ctx.pool.fetchrow(
        """
        WITH new_artifact AS (
            INSERT INTO observations (
                content, content_encrypted, about_user_id, confidence, significance, scoring_prompt_version,
                related_theme_ids, supporting_message_ids, last_reinforced_at, recorded_by_bot_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), $9)
            RETURNING id
        )
        INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
        SELECT 'observations', new_artifact.id, t.tid, $9, 'active', $11
        FROM new_artifact CROSS JOIN unnest($10::uuid[]) AS t(tid)
        RETURNING artifact_id AS id
        """,
        args.content,
        encrypt_value(args.content),
        args.about_user_id,
        args.confidence.value,
        significance,
        scoring_prompt_version,
        args.related_theme_ids,
        supporting_message_ids,
        ctx.bot_id,
        topic_id_list,
        args.reason,
    )
    result = LogObservationOutput(id=row["id"])
    await _sync_observation_embedding_after_create(ctx, row["id"])
    await _log_tool_call(ctx, "log_observation", logged_args, started, result)
    return result


async def update_observation(
    ctx: TurnContext, args: UpdateObservationInput
) -> UpdateObservationOutput:
    _solo_err = await _assert_solo_owns_row(
        ctx,
        table="observations",
        row_id=args.observation_id,
        owner_field="about_user_id",
    )
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    sets = ["last_reinforced_at=now()"]
    params: list[Any] = []
    for field in ("content", "confidence", "significance", "status", "related_theme_ids"):
        value = getattr(args, field)
        if value is not None:
            params.append(value.value if hasattr(value, "value") else value)
            sets.append(f"{field}=${len(params)}")
            if field == "content":
                params.append(encrypt_value(value))
                sets.append(f"content_encrypted=${len(params)}")
    params.append(args.observation_id)
    row = await ctx.pool.fetchrow(
        f"UPDATE observations SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id",
        *params,
    )
    result = UpdateObservationOutput(id=row["id"])
    await _sync_observation_embedding_after_update(ctx, row["id"])
    await _log_tool_call(ctx, "update_observation", args, started, result)
    return result


async def add_distillation(
    ctx: TurnContext, args: AddDistillationInput
) -> AddDistillationOutput:
    if (
        getattr(ctx, "bot_spec", None) is not None
        and ctx.bot_spec.participants_shape == "solo"
    ):
        for src in args.source_user_ids or []:
            _solo_err = _assert_solo_about_user(
                ctx, src, field_name="source_user_ids[*]"
            )
            if _solo_err is not None:
                raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    topic_slugs = resolve_write_topic_slugs(ctx, args.topic_slugs)
    require_reason_for_cross_topic(topic_slugs, ctx.primary_topic_slug, args.reason)
    topic_id_map = await resolve_topic_ids(ctx.pool, topic_slugs)
    topic_id_list = [topic_id_map[slug] for slug in topic_slugs]
    started = _start()
    supporting_message_ids = _default_supporting_message_ids(
        ctx, args.supporting_message_ids
    )
    logged_args = args.model_copy(
        update={"supporting_message_ids": supporting_message_ids}
    )
    try:
        await _require_existing_distillation_links(
            ctx,
            source_user_ids=args.source_user_ids,
            related_memory_ids=args.related_memory_ids,
            related_observation_ids=args.related_observation_ids,
            related_theme_ids=args.related_theme_ids,
            supporting_message_ids=supporting_message_ids,
        )
    except ToolCallRejected as exc:
        await _log_tool_call(ctx, "add_distillation", logged_args, started, exc.result)
        raise
    triggering_message_id = args.triggering_message_id
    if triggering_message_id is None and supporting_message_ids:
        triggering_message_id = supporting_message_ids[0]
    row = await ctx.pool.fetchrow(
        """
        WITH new_artifact AS (
            INSERT INTO distillations (
                content, content_encrypted, confidence, sensitivity, visibility,
                shareable_summary, shareable_summary_encrypted, source_user_ids,
                related_memory_ids, related_observation_ids, related_theme_ids,
                supporting_message_ids, triggering_message_id, recorded_by_bot_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid[], $9::uuid[], $10::uuid[], $11::uuid[], $12::uuid[], $13, $14)
            RETURNING id
        )
        INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
        SELECT 'distillations', new_artifact.id, t.tid, $14, 'active', $16
        FROM new_artifact CROSS JOIN unnest($15::uuid[]) AS t(tid)
        RETURNING artifact_id AS id
        """,
        args.content,
        encrypt_value(args.content),
        args.confidence.value,
        args.sensitivity.value,
        args.visibility.value,
        args.shareable_summary,
        (
            encrypt_value(args.shareable_summary)
            if args.shareable_summary is not None
            else None
        ),
        args.source_user_ids,
        args.related_memory_ids,
        args.related_observation_ids,
        args.related_theme_ids,
        supporting_message_ids,
        triggering_message_id,
        ctx.bot_id,
        topic_id_list,
        args.reason,
    )
    result = AddDistillationOutput(id=row["id"])
    await _sync_distillation_embedding_after_create(ctx, row["id"])
    await _log_tool_call(ctx, "add_distillation", logged_args, started, result)
    return result


async def update_distillation(
    ctx: TurnContext, args: UpdateDistillationInput
) -> UpdateDistillationOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    try:
        await _require_existing_distillation_links(
            ctx,
            source_user_ids=args.source_user_ids,
            related_memory_ids=args.related_memory_ids,
            related_observation_ids=args.related_observation_ids,
            related_theme_ids=args.related_theme_ids,
            supporting_message_ids=args.supporting_message_ids,
        )
    except ToolCallRejected as exc:
        await _log_tool_call(ctx, "update_distillation", args, started, exc.result)
        raise
    sets = ["updated_at=now()"]
    params: list[Any] = []
    for field in (
        "content",
        "confidence",
        "status",
        "sensitivity",
        "visibility",
        "shareable_summary",
        "source_user_ids",
        "related_memory_ids",
        "related_observation_ids",
        "related_theme_ids",
        "supporting_message_ids",
        "revision_note",
    ):
        field_value = getattr(args, field)
        if field_value is None:
            continue
        value = field_value.value if hasattr(field_value, "value") else field_value
        params.append(value)
        cast = "::uuid[]" if field.endswith("_ids") else ""
        sets.append(f"{field}=${len(params)}{cast}")
        if field == "content":
            params.append(encrypt_value(value))
            sets.append(f"content_encrypted=${len(params)}")
        elif field == "shareable_summary":
            params.append(encrypt_value(value))
            sets.append(f"shareable_summary_encrypted=${len(params)}")
    if args.status is not None and args.status.value == "retired":
        sets.append("retired_at=COALESCE(retired_at, now())")
    params.append(args.distillation_id)
    row = await ctx.pool.fetchrow(
        f"UPDATE distillations SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id",
        *params,
    )
    result = UpdateDistillationOutput(id=row["id"])
    await _sync_distillation_embedding_after_update(ctx, row["id"])
    await _log_tool_call(ctx, "update_distillation", args, started, result)
    return result


async def revise_distillation(
    ctx: TurnContext, args: ReviseDistillationInput
) -> ReviseDistillationOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    supporting_message_ids = _default_supporting_message_ids(
        ctx, args.supporting_message_ids
    )
    logged_args = args.model_copy(
        update={"supporting_message_ids": supporting_message_ids}
    )
    try:
        await _require_existing_distillation_links(
            ctx,
            source_user_ids=args.source_user_ids,
            related_memory_ids=args.related_memory_ids,
            related_observation_ids=args.related_observation_ids,
            related_theme_ids=args.related_theme_ids,
            supporting_message_ids=supporting_message_ids,
        )
    except ToolCallRejected as exc:
        await _log_tool_call(
            ctx, "revise_distillation", logged_args, started, exc.result
        )
        raise
    # Topic-scoped pre-check: ensure the old distillation belongs to this topic
    exists = await ctx.pool.fetchval(
        f"SELECT 1 FROM distillations d {join_artifact_topics('d', '$2')} WHERE d.id = $1 AND d.status = 'active'",
        args.old_distillation_id,
        ctx.primary_topic_id,
    )
    if not exists:
        raise ToolCallRejected(
            {
                "error": "distillation_revision_rejected",
                "reason": "old_distillation_id was not found, is not active, or is not scoped to this topic",
            }
        )
    triggering_message_id = args.triggering_message_id
    if triggering_message_id is None and supporting_message_ids:
        triggering_message_id = supporting_message_ids[0]
    row = await ctx.pool.fetchrow(
        f"""
        WITH old AS (
            SELECT d.id, d.revision_count
            FROM distillations d
            JOIN artifact_topics _at_d ON _at_d.artifact_table='distillations' AND _at_d.artifact_id=d.id AND _at_d.topic_id=$17 AND _at_d.status='active'
            WHERE d.id=$1 AND d.status='active'
        ),
        new AS (
            INSERT INTO distillations (
                content, content_encrypted, confidence, sensitivity, visibility,
                shareable_summary, shareable_summary_encrypted, source_user_ids,
                related_memory_ids, related_observation_ids, related_theme_ids,
                supporting_message_ids, triggering_message_id, supersedes_distillation_id,
                revision_note, revision_count, recorded_by_bot_id
            )
            SELECT $2, $3, $4, $5, $6, $7, $8, $9::uuid[], $10::uuid[], $11::uuid[], $12::uuid[],
                   $13::uuid[], $14, old.id, $15, old.revision_count + 1, $16
            FROM old
            RETURNING id, supersedes_distillation_id
        ),
        revised_old AS (
            UPDATE distillations d
            SET status='revised',
                superseded_by_distillation_id=new.id,
                revision_note=$15,
                revision_count=d.revision_count + 1,
                revised_at=now(),
                updated_at=now()
            FROM new
            WHERE d.id=new.supersedes_distillation_id
            RETURNING d.id
        ),
        topic_link AS (
            INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status)
            SELECT 'distillations', new.id, $17, $16, 'active'
            FROM new
        )
        SELECT new.id AS new_id, revised_old.id AS old_id
        FROM new
        JOIN revised_old ON revised_old.id = new.supersedes_distillation_id
        """,
        args.old_distillation_id,
        args.new_content,
        encrypt_value(args.new_content),
        args.confidence.value,
        args.sensitivity.value,
        args.visibility.value,
        args.shareable_summary,
        (
            encrypt_value(args.shareable_summary)
            if args.shareable_summary is not None
            else None
        ),
        args.source_user_ids,
        args.related_memory_ids,
        args.related_observation_ids,
        args.related_theme_ids,
        supporting_message_ids,
        triggering_message_id,
        args.revision_note,
        ctx.bot_id,
        ctx.primary_topic_id,
    )
    if row is None:
        result = {
            "error": "distillation_revision_rejected",
            "reason": "old_distillation_id was not found or is not active",
        }
        await _log_tool_call(ctx, "revise_distillation", logged_args, started, result)
        raise ToolCallRejected(result)
    result = ReviseDistillationOutput(new_id=row["new_id"], old_id=row["old_id"])
    await _sync_distillation_embedding_after_revise(
        ctx,
        old_distillation_id=row["old_id"],
        new_distillation_id=row["new_id"],
    )
    await _log_tool_call(ctx, "revise_distillation", logged_args, started, result)
    return result


async def add_oob(ctx: TurnContext, args: AddOOBInput) -> AddOOBOutput:
    _solo_err = _assert_solo_about_user(ctx, args.owner_id, field_name="owner_id")
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    topic_slugs = resolve_write_topic_slugs(ctx, args.topic_slugs)
    require_reason_for_cross_topic(topic_slugs, ctx.primary_topic_slug, args.reason)
    topic_id_map = await resolve_topic_ids(ctx.pool, topic_slugs)
    topic_id_list = [topic_id_map[slug] for slug in topic_slugs]
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        WITH new_artifact AS (
            INSERT INTO out_of_bounds (
                owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at, recorded_by_bot_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
        )
        INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
        SELECT 'out_of_bounds', new_artifact.id, t.tid, $7, 'active', $9
        FROM new_artifact CROSS JOIN unnest($8::uuid[]) AS t(tid)
        RETURNING artifact_id AS id
        """,
        args.owner_id,
        args.sensitive_core,
        encrypt_value(args.sensitive_core),
        args.shareable_context,
        args.severity.value,
        args.review_at,
        ctx.bot_id,
        topic_id_list,
        args.reason,
    )
    if args.review_at is not None:
        await _schedule_context_job(
            ctx.pool,
            user_id=args.owner_id,
            job_type="oob_review",
            scheduled_for=args.review_at,
            context_key="oob_id",
            context_id=row["id"],
            bot_id=ctx.bot_id,
            topic_id=ctx.primary_topic_id,
        )
    result = AddOOBOutput(id=row["id"])
    await _log_tool_call(ctx, "add_oob", args, started, result)
    return result


async def update_oob(ctx: TurnContext, args: UpdateOOBInput) -> UpdateOOBOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    sets: list[str] = []
    params: list[Any] = []
    for field in ("sensitive_core", "shareable_context", "severity", "review_at"):
        value = getattr(args, field)
        if value is not None:
            params.append(value.value if hasattr(value, "value") else value)
            sets.append(f"{field}=${len(params)}")
            if field == "sensitive_core":
                params.append(encrypt_value(value))
                sets.append(f"sensitive_core_encrypted=${len(params)}")
    if not sets:
        sets.append("sensitive_core=sensitive_core")
    params.append(args.oob_id)
    row = await ctx.pool.fetchrow(
        f"UPDATE out_of_bounds SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id",
        *params,
    )
    if args.review_at is not None:
        owner_id = await ctx.pool.fetchval(
            f"SELECT x.owner_id FROM out_of_bounds x {join_artifact_topics('x', '$2')} WHERE x.id=$1",
            args.oob_id,
            ctx.primary_topic_id,
        )
        await _schedule_context_job(
            ctx.pool,
            user_id=owner_id,
            job_type="oob_review",
            scheduled_for=args.review_at,
            context_key="oob_id",
            context_id=args.oob_id,
            bot_id=ctx.bot_id,
            topic_id=ctx.primary_topic_id,
        )
    result = UpdateOOBOutput(id=row["id"])
    await _log_tool_call(ctx, "update_oob", args, started, result)
    return result


async def lift_oob(ctx: TurnContext, args: LiftOOBInput) -> LiftOOBOutput:
    _solo_err = await _assert_solo_owns_row(
        ctx, table="out_of_bounds", row_id=args.oob_id, owner_field="owner_id"
    )
    if _solo_err is not None:
        raise ToolCallRejected(_solo_err)
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    row = await ctx.pool.fetchrow(
        "UPDATE out_of_bounds SET status='lifted' WHERE id=$1 RETURNING id, now() AS lifted_at",
        args.oob_id,
    )
    result = LiftOOBOutput(id=row["id"], lifted_at=row["lifted_at"])
    await _log_tool_call(ctx, "lift_oob", args, started, result)
    return result


async def schedule_checkin(
    ctx: TurnContext, args: ScheduleCheckinInput
) -> ScheduleCheckinOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    scheduled_for = _scheduled_for_from_schedule_fields(
        ctx, args.when, args.delay, args.local_when
    )
    old, row = await schedule_checkin_record(
        ctx.pool,
        args.user_id,
        scheduled_for=scheduled_for,
        context={"about_what": args.about_what, "reason": args.reason},
        bot_id=ctx.bot_id,
        topic_id=ctx.primary_topic_id,
    )
    result = ScheduleCheckinOutput(
        job_id=row["job_id"],
        superseded_job_id=old["id"] if old is not None else None,
        scheduled_for=row["scheduled_for"],
    )
    await _log_tool_call(ctx, "schedule_checkin", args, started, result)
    return result


async def cancel_scheduled_checkin(
    ctx: TurnContext, args: CancelScheduledCheckinInput
) -> CancelScheduledCheckinOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        UPDATE scheduled_jobs
        SET status='cancelled'
        WHERE user_id=$1 AND job_type='checkin' AND status='pending'
        RETURNING id
        """,
        args.user_id,
    )
    result = CancelScheduledCheckinOutput(
        action="cancelled" if row is not None else "noop",
        cancelled_job_id=row["id"] if row is not None else None,
    )
    await _log_tool_call(ctx, "cancel_scheduled_checkin", args, started, result)
    return result


# ---------------------------------------------------------------------------
# PARTNER WRITE EXCEPTION (SD-009)
# ---------------------------------------------------------------------------
# `schedule_partner_checkin` is the only WRITE_PHASE_TOOLS verb that inserts a
# scheduled_jobs row whose ``user_id`` is NOT ``ctx.user.id``. Safety derives
# from four belts-and-braces:
#
#   1. The Pydantic schema has NO ``user_id`` / ``target_user_id`` field
#      (invariant 2). A hallucinated id cannot redirect the write.
#   2. The partner is resolved server-side via ``resolve_dyad_partner``
#      from the canonical dyads/dyad_members tables.
#   3. The recipient's per-bot ``partner_share`` MUST be ``opt_in``;
#      ``opt_out`` and ``pending`` (None) hard-block (invariant 3, SD-003).
#   4. ``bot_id`` and ``topic_id`` on the new row stay tied to the
#      originator's ``ctx``; the recipient's bot+topic remain untouched.
#
# ``reason`` is audit-only (invariant 4) — never rendered in any prompt
# or hot context. ``nudge_note`` is recipient-visible and capped at 300
# chars by the Input schema.
# ---------------------------------------------------------------------------


def _is_unique_violation(exc: Exception) -> bool:
    return (
        exc.__class__.__name__ == "UniqueViolationError"
        or getattr(exc, "sqlstate", None) == "23505"
        or getattr(exc, "pgcode", None) == "23505"
    )


async def schedule_partner_checkin(
    ctx: TurnContext, args: SchedulePartnerCheckinInput
) -> SchedulePartnerCheckinOutput:
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()

    # 1. Resolve the dyad partner — backend-only, never from input.
    partner = await resolve_dyad_partner(ctx.pool, ctx.user.id)
    if partner is None:
        raise ToolCallRejected({"error": "no_dyad_partner"})

    # 2. Recipient gating. opt_out AND pending (None) both hard-block.
    if ctx.bot_id is None:
        raise ToolCallRejected({"error": "missing_bot_id"})
    recipient_share = await get_partner_share(
        ctx.pool, user_id=partner.partner_user_id, bot_id=ctx.bot_id
    )
    if recipient_share != "opt_in":
        raise ToolCallRejected(
            {
                "error": "recipient_not_opted_in",
                "recipient_state": recipient_share or "pending",
            }
        )

    # 3. 24h code rate limit (SD-007). Counts ANY status (pending,
    # completed, cancelled, superseded) so fire-and-replace abuse is
    # blocked. Stricter than the unique-pending DB index, which only
    # guards against simultaneous-pending stacking.
    nudge_count = await ctx.pool.fetchval(
        """
        SELECT count(*) FROM scheduled_jobs
        WHERE bot_id=$1
          AND job_type='scheduled_task'
          AND context->>'kind'='partner_nudge'
          AND context->>'originating_user_id'=$2
          AND created_at > now() - interval '24 hours'
        """,
        ctx.bot_id,
        str(ctx.user.id),
    )
    if nudge_count and int(nudge_count) >= 1:
        raise ToolCallRejected(
            {"error": "rate_limited", "window_hours": 24}
        )

    # 4. Resolve concrete fire time and insert.
    scheduled_for = _scheduled_for_from_schedule_fields(
        ctx, args.when, args.delay, args.local_when
    )
    context_jsonb: dict[str, Any] = {
        "kind": "partner_nudge",
        "originating_user_id": str(ctx.user.id),
        "nudge_note": args.nudge_note,
        # reason is audit-only — stored but never rendered.
        "reason": args.reason,
        "source": args.source,
    }
    try:
        row = await ctx.pool.fetchrow(
            """
            INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
            VALUES ($1, 'scheduled_task', $2, $3::jsonb, 'pending', $4, $5)
            RETURNING id AS job_id, scheduled_for, context
            """,
            partner.partner_user_id,
            scheduled_for,
            context_jsonb,
            ctx.bot_id,
            ctx.primary_topic_id,
        )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ToolCallRejected(
                {"error": "duplicate_pending_nudge"}
            ) from exc
        raise

    result = SchedulePartnerCheckinOutput(
        job_id=row["job_id"],
        scheduled_for=row["scheduled_for"],
        recipient_user_id=partner.partner_user_id,
    )
    await _log_tool_call(ctx, "schedule_partner_checkin", args, started, result)
    return result


async def cancel_partner_nudge(
    ctx: TurnContext, args: CancelPartnerNudgeInput
) -> CancelPartnerNudgeOutput:
    """Originator-only cancellation (SD-010).

    Only the user who scheduled the nudge can cancel it, and only while
    it is still pending. The row is matched by job_id; the WHERE clause
    enforces ``context.originating_user_id == ctx.user.id`` so a
    different user cannot cancel someone else's nudge even with a
    leaked id.
    """
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    existing = await ctx.pool.fetchrow(
        """
        SELECT id, status, context
        FROM scheduled_jobs
        WHERE id=$1 AND job_type='scheduled_task'
        """,
        args.job_id,
    )
    if existing is None:
        raise ToolCallRejected({"error": "not_found"})
    context = existing.get("context") or {}
    if str(context.get("originating_user_id")) != str(ctx.user.id):
        raise ToolCallRejected({"error": "not_owner"})
    if existing.get("status") != "pending":
        raise ToolCallRejected(
            {"error": "not_pending", "status": existing.get("status")}
        )
    row = await ctx.pool.fetchrow(
        """
        UPDATE scheduled_jobs
        SET status='cancelled'
        WHERE id=$1
          AND status='pending'
          AND context->>'originating_user_id'=$2
          AND context->>'kind'='partner_nudge'
        RETURNING id
        """,
        args.job_id,
        str(ctx.user.id),
    )
    result = CancelPartnerNudgeOutput(
        action="cancelled" if row is not None else "noop",
        cancelled_job_id=row["id"] if row is not None else None,
    )
    await _log_tool_call(ctx, "cancel_partner_nudge", args, started, result)
    return result


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _future_utc(value: datetime, *, field_name: str = "when") -> datetime:
    scheduled_for = _as_utc(value)
    if scheduled_for <= datetime.now(UTC):
        raise ToolCallRejected({"error": "schedule_time_in_past", "field": field_name})
    return scheduled_for


def _reject_utc_when_for_local_user(ctx: TurnContext, when: datetime) -> None:
    user_timezone = str(getattr(ctx.user, "timezone", None) or "UTC")
    if user_timezone.upper() in {"UTC", "ETC/UTC"}:
        return
    if when.utcoffset() == timedelta(0):
        raise ToolCallRejected(
            {
                "error": "use_local_when_for_user_local_time",
                "field": "when",
                "timezone": user_timezone,
                "hint": "For local clock phrases like '9pm tonight', call the tool again with local_when instead of UTC when.",
            }
        )


def _delay_delta(delay: ScheduleDelay) -> timedelta:
    return timedelta(
        weeks=delay.weeks, days=delay.days, hours=delay.hours, minutes=delay.minutes
    )


def _timezone_for_local_schedule(
    ctx: TurnContext, timezone_name: str | None
) -> ZoneInfo:
    requested = timezone_name or getattr(ctx.user, "timezone", None) or "UTC"
    try:
        return ZoneInfo(str(requested))
    except ZoneInfoNotFoundError as exc:
        raise ToolCallRejected(
            {"error": "invalid_timezone", "timezone": str(requested)}
        ) from exc


def _local_when_to_utc(ctx: TurnContext, local_when: LocalScheduleTime) -> datetime:
    tz = _timezone_for_local_schedule(ctx, local_when.timezone)
    local_dt = datetime.combine(local_when.date, local_when.time).replace(tzinfo=tz)
    return local_dt.astimezone(UTC)


def _scheduled_for_from_local_when(
    ctx: TurnContext, local_when: LocalScheduleTime
) -> datetime:
    return _future_utc(_local_when_to_utc(ctx, local_when), field_name="local_when")


def _scheduled_for_from_schedule_fields(
    ctx: TurnContext,
    when: datetime | None,
    delay: ScheduleDelay | None,
    local_when: LocalScheduleTime | None,
) -> datetime:
    if delay is not None:
        return datetime.now(UTC) + _delay_delta(delay)
    if local_when is not None:
        return _scheduled_for_from_local_when(ctx, local_when)
    if when is None:
        raise ToolCallRejected({"error": "missing_schedule_time", "field": "when"})
    _reject_utc_when_for_local_user(ctx, when)
    return _future_utc(when)


def _scheduled_task_recurrence_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return normalize_recurrence(raw)


def _scheduled_task_context(
    *, task_id: Any, brief: str, recurrence: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "task_id": str(task_id),
        "brief": brief,
        "recurrence": recurrence,
    }


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if value is None else value


def _scheduled_task_row(row: Any, ctx: TurnContext) -> ScheduledTaskRow:
    context = _row_value(row, "context", {})
    now = ctx.turn_started_at or datetime.now(UTC)
    timezone = ctx.user.timezone or "UTC"
    recurrence = context.get("recurrence")
    recurrence_until = recurrence.get("until") if isinstance(recurrence, dict) else None
    return ScheduledTaskRow(
        task_id=context["task_id"],
        job_id=_row_value(row, "job_id", _row_value(row, "id")),
        brief=context["brief"],
        scheduled_for=row["scheduled_for"],
        scheduled_for_time=temporal_reference(row["scheduled_for"], timezone, now=now),
        recurrence=recurrence,
        recurrence_until_time=temporal_reference(
            (
                datetime.fromisoformat(recurrence_until)
                if isinstance(recurrence_until, str)
                else recurrence_until
            ),
            timezone,
            now=now,
        ),
        delayed=bool(_row_value(row, "delayed", False)),
        created_at=_row_value(row, "created_at"),
        created_at_time=temporal_reference(
            _row_value(row, "created_at"), timezone, now=now
        ),
    )


def _scheduled_task_target(
    args: UpdateScheduledTaskInput | CancelScheduledTaskInput, ctx: TurnContext
) -> tuple[Any | None, str | None]:
    if args.current_task:
        current = current_scheduled_task(ctx)
        if current is None:
            raise ToolCallRejected(
                {
                    "error": "current_task=true is only valid during a scheduled_task turn"
                }
            )
        return current["job_id"], None
    return args.job_id, str(args.task_id) if args.task_id is not None else None


async def list_scheduled_tasks(
    ctx: TurnContext, args: ListScheduledTasksInput
) -> ListScheduledTasksOutput:
    started = _start()
    rows = await ctx.pool.fetch(
        """
        SELECT id AS job_id, scheduled_for, context, delayed, created_at
        FROM scheduled_jobs
        WHERE user_id=$1
          AND job_type='scheduled_task'
          AND status='pending'
          AND ($2::boolean OR context->'recurrence' IS NULL OR context->'recurrence' = 'null'::jsonb)
        ORDER BY scheduled_for ASC, created_at ASC
        LIMIT $3
        """,
        ctx.user.id,
        args.include_recurring,
        args.limit,
    )
    result = ListScheduledTasksOutput(
        tasks=[_scheduled_task_row(row, ctx) for row in rows]
    )
    await _log_tool_call_shared(
        ctx, "list_scheduled_tasks", args, started, result, kind="read"
    )
    return result


async def schedule_task(
    ctx: TurnContext, args: ScheduleTaskInput
) -> ScheduleTaskOutput:
    started = _start()
    task_id = uuid4()
    recurrence = _scheduled_task_recurrence_payload(args.recurrence)
    scheduled_for = _scheduled_for_from_schedule_fields(
        ctx, args.when, args.delay, args.local_when
    )
    context = _scheduled_task_context(
        task_id=task_id, brief=args.brief, recurrence=recurrence
    )
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
        VALUES ($1, 'scheduled_task', $2, $3::jsonb, 'pending', $4, $5)
        RETURNING id AS job_id, scheduled_for, context
        """,
        ctx.user.id,
        scheduled_for,
        context,
        ctx.bot_id,
        ctx.primary_topic_id,
    )
    result = ScheduleTaskOutput(
        task_id=task_id,
        job_id=row["job_id"],
        scheduled_for=row["scheduled_for"],
        recurrence=recurrence,
    )
    await _log_tool_call(ctx, "schedule_task", args, started, result)
    return result


def _reject_unauthorized_current_scheduled_task(
    ctx: TurnContext, current_task: bool
) -> None:
    if current_task and current_scheduled_task(ctx) is None:
        raise ToolCallRejected(
            {"error": "current_task=true is only valid during a scheduled_task turn"}
        )


async def update_scheduled_task(
    ctx: TurnContext, args: UpdateScheduledTaskInput
) -> UpdateScheduledTaskOutput:
    started = _start()
    target_job_id, target_task_id = _scheduled_task_target(args, ctx)
    scheduled_for = (
        _scheduled_for_from_schedule_fields(ctx, args.when, args.delay, args.local_when)
        if args.when is not None
        or args.delay is not None
        or args.local_when is not None
        else None
    )
    context_patch: dict[str, Any] = {}
    if args.brief is not None:
        context_patch["brief"] = args.brief
    recurrence_was_set = "recurrence" in args.model_fields_set
    if recurrence_was_set:
        context_patch["recurrence"] = _scheduled_task_recurrence_payload(
            args.recurrence
        )
    row = await ctx.pool.fetchrow(
        """
        UPDATE scheduled_jobs
        SET scheduled_for=COALESCE($4, scheduled_for),
            context=COALESCE(context, '{}'::jsonb) || $5::jsonb,
            updated_at=now()
        WHERE user_id=$1
          AND job_type='scheduled_task'
          AND status='pending'
          AND (($2::uuid IS NOT NULL AND id=$2) OR ($3::text IS NOT NULL AND context->>'task_id'=$3))
        RETURNING id AS job_id, scheduled_for, context
        """,
        ctx.user.id,
        target_job_id,
        target_task_id,
        scheduled_for,
        context_patch,
    )
    if row is None:
        result = UpdateScheduledTaskOutput(
            action="noop", job_id=target_job_id, task_id=target_task_id
        )
    else:
        task = _scheduled_task_row(row, ctx)
        result = UpdateScheduledTaskOutput(
            action="updated",
            task_id=task.task_id,
            job_id=task.job_id,
            scheduled_for=task.scheduled_for,
            recurrence=task.recurrence,
        )
    await _log_tool_call(ctx, "update_scheduled_task", args, started, result)
    return result


async def update_scheduled_checkin(
    ctx: TurnContext, args: UpdateScheduledCheckinInput
) -> UpdateScheduledCheckinOutput:
    """Update a pending user-facing check-in by job_id.

    Mirrors ``update_scheduled_task`` for one-off user-facing reminders.
    Updates time (when/delay/local_when), ``about_what`` (the user-facing
    line stored in ``context['about_what']``) and/or ``reason`` (audit-only,
    stored in ``context['reason']``).

    Returns ``action='noop'`` when no matching row is found — this single
    check covers other-user rows, scheduled_task rows, cancelled/fired rows,
    and out-of-scope rows.
    """
    started = _start()
    scheduled_for: datetime | None = None
    if (
        args.when is not None
        or args.delay is not None
        or args.local_when is not None
    ):
        scheduled_for = _scheduled_for_from_schedule_fields(
            ctx, args.when, args.delay, args.local_when
        )
    context_patch: dict[str, Any] = {}
    if args.about_what is not None:
        context_patch["about_what"] = args.about_what
    if args.reason is not None:
        context_patch["reason"] = args.reason
    row = await ctx.pool.fetchrow(
        """
        UPDATE scheduled_jobs
        SET scheduled_for=COALESCE($5, scheduled_for),
            context=COALESCE(context, '{}'::jsonb) || $6::jsonb,
            updated_at=now()
        WHERE user_id=$1
          AND id=$2
          AND job_type='checkin'
          AND status='pending'
          AND bot_id=$3
          AND topic_id=$4
        RETURNING id AS job_id, scheduled_for, context
        """,
        ctx.user.id,
        args.job_id,
        ctx.bot_id,
        ctx.primary_topic_id,
        scheduled_for,
        context_patch,
    )
    if row is None:
        result = UpdateScheduledCheckinOutput(
            action="noop", job_id=args.job_id
        )
    else:
        updated_context = row["context"] or {}
        result = UpdateScheduledCheckinOutput(
            action="updated",
            job_id=row["job_id"],
            scheduled_for=row["scheduled_for"],
            about_what=updated_context.get("about_what"),
        )
    await _log_tool_call(ctx, "update_scheduled_checkin", args, started, result)
    return result


async def cancel_scheduled_task(
    ctx: TurnContext, args: CancelScheduledTaskInput
) -> CancelScheduledTaskOutput:
    started = _start()
    target_job_id, target_task_id = _scheduled_task_target(args, ctx)
    if args.current_task:
        row = await ctx.pool.fetchrow(
            """
            UPDATE scheduled_jobs
            SET context=COALESCE(context, '{}'::jsonb) || $3::jsonb,
                updated_at=now()
            WHERE user_id=$1
              AND job_type='scheduled_task'
              AND status='pending'
              AND id=$2
            RETURNING id AS job_id, context
            """,
            ctx.user.id,
            target_job_id,
            {
                "scheduled_task_control": {
                    "cancel_after_current_fire": True,
                    "reason": args.reason,
                }
            },
        )
    else:
        row = await ctx.pool.fetchrow(
            """
            UPDATE scheduled_jobs
            SET status='cancelled',
                cancellation_reason=$4,
                updated_at=now()
            WHERE user_id=$1
              AND job_type='scheduled_task'
              AND status='pending'
              AND (($2::uuid IS NOT NULL AND id=$2) OR ($3::text IS NOT NULL AND context->>'task_id'=$3))
            RETURNING id AS job_id, context
            """,
            ctx.user.id,
            target_job_id,
            target_task_id,
            args.reason,
        )
    if row is None:
        result = CancelScheduledTaskOutput(
            action="noop", job_id=target_job_id, task_id=target_task_id
        )
    else:
        context = row["context"]
        result = CancelScheduledTaskOutput(
            action="cancelled",
            job_id=row["job_id"],
            task_id=context.get("task_id"),
        )
    await _log_tool_call(ctx, "cancel_scheduled_task", args, started, result)
    return result


async def escalate_to_partner(
    ctx: TurnContext, args: EscalateToPartnerInput
) -> EscalateToPartnerOutput:
    started = _start()
    if args.from_user_id != ctx.user.id or args.to_user_id != ctx.partner.id:
        logger.warning(
            "escalate_to_partner overriding model-supplied IDs for turn_id=%s",
            ctx.turn_id,
            extra=obs_fields(ctx),
        )
    allowed_by_crisis = ctx.trigger_charge == "crisis"
    allowed_by_explicit_request = ctx.explicit_partner_alert_requested
    if not allowed_by_crisis and not allowed_by_explicit_request:
        result = {
            "error": "escalation_rejected",
            "reason": "escalate_to_partner requires trusted crisis charge or explicit partner-alert request before sending",
        }
        await _log_tool_call(ctx, "escalate_to_partner", args, started, result)
        raise ToolCallRejected(result)
    template = TemplateCall(
        name="escalation", params=[ctx.partner.name, ctx.user.name, args.content]
    )
    out_id = await send_outbound(
        ctx.pool,
        ctx.partner,
        args.content,
        template_fallback=template,
        bot_turn_id=ctx.turn_id,
        protected_owner_ids=[ctx.user.id, ctx.partner.id],
        scope=scope_from_turn_context(ctx),
    )
    await _append_turn_reasoning(
        ctx.pool,
        ctx.turn_id,
        f"ESCALATION_SENT gate={'crisis' if allowed_by_crisis else 'explicit_partner_alert'} reason={args.reason} outbound_message_id={out_id}",
    )
    result = EscalateToPartnerOutput(
        action="sent",
        outbound_message_id=out_id,
        used_template=False,
        reason_if_deferred=None,
    )
    await _log_tool_call(ctx, "escalate_to_partner", args, started, result)
    return result


async def _fetch_dyad_message(ctx: TurnContext, message_id: Any) -> Any | None:
    return await ctx.pool.fetchrow(
        """
        SELECT id, direction, sender_id, recipient_id, content, whatsapp_message_id, deleted_at, media_analysis
        FROM messages
        WHERE id=$1
          AND (
            sender_id = ANY($2::uuid[])
            OR recipient_id = ANY($2::uuid[])
          )
        """,
        message_id,
        [ctx.user.id, ctx.partner.id],
    )


async def edit_outbound_message(
    ctx: TurnContext, args: EditOutboundMessageInput
) -> EditOutboundMessageOutput:
    started = _start()
    if ctx.partner is None:
        logger.warning(
            "edit_outbound_message called with solo bot (no partner); unsupported",
            extra=obs_fields(ctx),
        )
        result = EditOutboundMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=None,
            reason="message editing is not available for solo bots",
        )
        await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
        return result

    row = await _fetch_dyad_message(ctx, args.message_id)
    if (
        row is None
        or row["direction"] != "outbound"
        or row["recipient_id"] not in {ctx.user.id, ctx.partner.id}
        or row["whatsapp_message_id"] is None
        or row["deleted_at"] is not None
    ):
        result = EditOutboundMessageOutput(
            action="not_found",
            message_id=args.message_id,
            reason="message is not an editable, delivered bot outbound in this conversation",
        )
        await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
        return result

    if get_settings().messaging_provider.strip().lower() != "discord":
        result = EditOutboundMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            reason="editing already-sent bot messages is currently implemented only for Discord",
        )
        await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
        return result

    turn_scope = scope_from_turn_context(ctx)
    verdict = await _call_oob_hook(
        ctx.pool,
        args.content,
        row["recipient_id"],
        [ctx.user.id, ctx.partner.id],
        bot_id=turn_scope.bot_id,
        topic_id=turn_scope.topic_id,
    )
    if verdict["verdict"] != "ok":
        result = EditOutboundMessageOutput(
            action="blocked",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            reason=verdict["reason"],
            suggested_rewrite=verdict.get("suggested_rewrite"),
        )
        await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
        return result

    recipient_phone = (
        ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    )
    await discord.edit_text(
        recipient_phone,
        row["whatsapp_message_id"],
        args.content,
        bot_id=turn_scope.bot_id,
    )
    await ctx.pool.execute(
        """
        UPDATE messages
        SET edit_history = COALESCE(edit_history, '[]'::jsonb)
                || jsonb_build_array(jsonb_build_object('content', content, 'at', now(), 'reason', $1)),
            content = $2,
            content_encrypted = $3,
            edited_at = now()
        WHERE id = $4
        """,
        args.reason,
        args.content,
        encrypt_value(args.content),
        args.message_id,
    )
    await enqueue_message_reembed(
        ctx.pool,
        message_id=args.message_id,
        content=args.content,
        media_analysis=row["media_analysis"],
    )
    result = EditOutboundMessageOutput(
        action="edited",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        reason=args.reason,
    )
    await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
    return result


async def delete_outbound_message(
    ctx: TurnContext, args: DeleteOutboundMessageInput
) -> DeleteOutboundMessageOutput:
    started = _start()
    if ctx.partner is None:
        logger.warning(
            "delete_outbound_message called with solo bot (no partner); unsupported",
            extra=obs_fields(ctx),
        )
        result = DeleteOutboundMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            reason="message deletion is not available for solo bots",
        )
        await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
        return result

    row = await _fetch_dyad_message(ctx, args.message_id)
    if (
        row is None
        or row["direction"] != "outbound"
        or row["recipient_id"] not in {ctx.user.id, ctx.partner.id}
        or row["whatsapp_message_id"] is None
        or row["deleted_at"] is not None
    ):
        result = DeleteOutboundMessageOutput(
            action="not_found",
            message_id=args.message_id,
            reason="message is not a deletable, delivered bot outbound in this conversation",
        )
        await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
        return result

    if get_settings().messaging_provider.strip().lower() != "discord":
        result = DeleteOutboundMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            reason="deleting already-sent bot messages is currently implemented only for Discord",
        )
        await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
        return result

    recipient_phone = (
        ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    )
    turn_scope = scope_from_turn_context(ctx)
    await discord.delete_text(
        recipient_phone, row["whatsapp_message_id"], bot_id=turn_scope.bot_id
    )
    await ctx.pool.execute(
        "UPDATE messages SET deleted_at = now(), processing_state='expired' WHERE id=$1",
        args.message_id,
    )
    await enqueue_message_embedding_drop(ctx.pool, message_id=args.message_id)
    result = DeleteOutboundMessageOutput(
        action="deleted",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        reason=args.reason,
    )
    await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
    return result


async def react_to_message(
    ctx: TurnContext, args: ReactToMessageInput
) -> ReactToMessageOutput:
    started = _start()
    if ctx.partner is None:
        logger.warning(
            "react_to_message called with solo bot (no partner); unsupported",
            extra=obs_fields(ctx),
        )
        result = ReactToMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=None,
            emoji=args.emoji,
            reason="bot reactions are not available for solo bots",
        )
        await _log_tool_call(ctx, "react_to_message", args, started, result)
        return result

    row = await _fetch_dyad_message(ctx, args.message_id)
    if (
        row is None
        or row["whatsapp_message_id"] is None
        or row["deleted_at"] is not None
    ):
        result = ReactToMessageOutput(
            action="not_found",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"] if row is not None else None,
            emoji=args.emoji,
            reason="message is not a delivered, visible message in this conversation",
        )
        await _log_tool_call(ctx, "react_to_message", args, started, result)
        return result

    if get_settings().messaging_provider.strip().lower() != "discord":
        result = ReactToMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            emoji=args.emoji,
            reason="bot reactions are currently implemented only for Discord",
        )
        await _log_tool_call(ctx, "react_to_message", args, started, result)
        return result

    if row["direction"] == "inbound":
        target_phone = (
            ctx.user.phone if row["sender_id"] == ctx.user.id else ctx.partner.phone
        )
    else:
        target_phone = (
            ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
        )
    turn_scope = scope_from_turn_context(ctx)
    await discord.add_reaction(
        target_phone, row["whatsapp_message_id"], args.emoji, bot_id=turn_scope.bot_id
    )
    result = ReactToMessageOutput(
        action="reacted",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        emoji=args.emoji,
        reason=args.reason,
    )
    await _log_tool_call(ctx, "react_to_message", args, started, result)
    return result


async def explain_media_item(
    ctx: TurnContext, args: ExplainMediaItemInput
) -> ExplainMediaItemOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        SELECT id, direction, sender_id, recipient_id, content, media_type, media_url, media_analysis, deleted_at, bot_id
        FROM messages
        WHERE id=$1
          AND (
            sender_id = ANY($2::uuid[])
            OR recipient_id = ANY($2::uuid[])
          )
          AND bot_id=$3
          AND topic_id=$4
        """,
        args.message_id,
        [ctx.user.id, ctx.partner.id],
        ctx.bot_id,
        ctx.primary_topic_id,
    )
    if row is None or row["deleted_at"] is not None:
        result = ExplainMediaItemOutput(
            action="not_found", message_id=args.message_id, reason="message not found"
        )
        await _log_tool_call(ctx, "explain_media_item", args, started, result)
        return result
    owner_id = _message_thread_owner_id(row)
    partner_share = "unset"
    if owner_id == ctx.user.id:
        partner_share = "opt_in"
    elif row["bot_id"] is not None:
        partner_share = await _partner_share_for_bot(ctx, owner_id, row["bot_id"])
    if not raw_message_visibility(
        viewer_user_id=ctx.user.id,
        thread_owner_user_id=owner_id,
        thread_owner_partner_share=partner_share,
    ).visible:
        result = ExplainMediaItemOutput(
            action="blocked",
            message_id=args.message_id,
            media_type=row["media_type"],
            reason="raw partner media hidden by partner_share",
        )
        await _log_tool_call(ctx, "explain_media_item", args, started, result)
        return result
    if row["media_type"] != "image" or not row["media_url"]:
        result = ExplainMediaItemOutput(
            action="unsupported",
            message_id=args.message_id,
            media_type=row["media_type"],
            reason="only stored image media can be explained right now",
        )
        await _log_tool_call(ctx, "explain_media_item", args, started, result)
        return result

    try:
        analysis = await explain_stored_image(ctx.pool, args.message_id)
    except Exception as exc:
        result = ExplainMediaItemOutput(
            action="unsupported",
            message_id=args.message_id,
            media_type=row["media_type"],
            reason=f"media explanation failed: {exc}",
        )
        await _log_tool_call(ctx, "explain_media_item", args, started, result)
        return result

    result = ExplainMediaItemOutput(
        action="explained",
        message_id=args.message_id,
        media_type=row["media_type"],
        explanation=analysis.get("explanation") or analysis.get("description"),
        reason=args.reason,
    )
    await enqueue_message_reembed(
        ctx.pool,
        message_id=args.message_id,
        content=row["content"],
        media_analysis=analysis,
    )
    await _log_tool_call(ctx, "explain_media_item", args, started, result)
    return result


async def log_feedback(ctx: TurnContext, args: LogFeedbackInput) -> LogFeedbackOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO feedback (from_user_id, target_type, target_id, sentiment, content, source, bot_id, topic_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        args.from_user_id,
        args.target_type,
        args.target_id,
        args.sentiment.value,
        args.content,
        args.source,
        ctx.bot_id,
        ctx.primary_topic_id,
    )
    result = LogFeedbackOutput(id=row["id"])
    await _log_tool_call(ctx, "log_feedback", args, started, result)
    return result


# ---------------------------------------------------------------------------
# Pregnancy write tools (Tante Rosi)
# ---------------------------------------------------------------------------


async def set_pregnancy_edd(
    ctx: TurnContext, args: "SetPregnancyEddInput"
) -> "SetPregnancyEddOutput":
    """Initial pregnancy capture. Requires no active pregnancy on ctx.user."""
    from datetime import date as _date, datetime as _datetime

    from app.services.pregnancy import gestational_age

    started = _start()
    edd_val = _date.fromisoformat(args.edd)
    _check_edd_window(edd_val)

    row = await ctx.pool.fetchrow(
        "SELECT id, pregnancy_edd, pregnancy_ended_at FROM users WHERE id = $1 FOR UPDATE",
        ctx.user.id,
    )
    if row["pregnancy_edd"] is not None and row["pregnancy_ended_at"] is None:
        raise ToolCallRejected(
            {
                "error": "pregnancy_already_active",
                "reason": (
                    "There is already an active pregnancy (EDD "
                    f"{row['pregnancy_edd'].isoformat()}). "
                    "Use correct_pregnancy_edd to revise or end_pregnancy to close it first."
                ),
            }
        )

    lmp_date = _date.fromisoformat(args.lmp_date) if args.lmp_date else None
    scan_date = _date.fromisoformat(args.scan_date) if args.scan_date else None
    if args.started_at:
        started_at = _datetime.fromisoformat(args.started_at)
    else:
        started_at = _datetime.now(UTC)

    await ctx.pool.fetchrow(
        """
        UPDATE users
        SET pregnancy_edd = $2,
            pregnancy_dating_basis = $3,
            pregnancy_lmp_date = $4,
            pregnancy_scan_date = $5,
            pregnancy_started_at = $6
        WHERE id = $1
        RETURNING id
        """,
        ctx.user.id,
        edd_val,
        args.dating_basis,
        lmp_date,
        scan_date,
        started_at,
    )

    weeks, days = gestational_age(edd_val)
    ga_str = f"{weeks}w{days}d"

    # Refresh ctx.user in-memory (dataclasses.replace — avoids deadlock)
    from dataclasses import replace as _replace

    ctx.user = _replace(
        ctx.user,
        pregnancy_edd=edd_val,
        pregnancy_dating_basis=args.dating_basis,
        pregnancy_lmp_date=lmp_date,
        pregnancy_scan_date=scan_date,
        pregnancy_started_at=started_at,
    )

    result = SetPregnancyEddOutput(ok=True, edd=args.edd, gestational_age=ga_str)
    await _log_tool_call(ctx, "set_pregnancy_edd", args, started, result)
    return result


async def correct_pregnancy_edd(
    ctx: TurnContext,
    args: "CorrectPregnancyEddInput",
) -> "CorrectPregnancyEddOutput":
    """Mid-pregnancy EDD revision (e.g. after a dating scan). Requires an active pregnancy."""
    from datetime import date as _date, datetime as _datetime

    from app.services.pregnancy import gestational_age

    started = _start()
    edd_val = _date.fromisoformat(args.edd)
    _check_edd_window(edd_val)

    row = await ctx.pool.fetchrow(
        "SELECT id, pregnancy_edd, pregnancy_ended_at, pregnancy_dating_basis, pregnancy_started_at FROM users WHERE id = $1 FOR UPDATE",
        ctx.user.id,
    )
    if row["pregnancy_edd"] is None or row["pregnancy_ended_at"] is not None:
        raise ToolCallRejected(
            {
                "error": "no_active_pregnancy",
                "reason": (
                    "There is no active pregnancy to correct. "
                    "Use set_pregnancy_edd to start tracking a pregnancy first."
                ),
            }
        )

    scan_date = _date.fromisoformat(args.scan_date) if args.scan_date else None
    scan_corrected_at = (
        _datetime.now(UTC)
        if args.dating_basis == "scan" and row["pregnancy_dating_basis"] != "scan"
        else None
    )

    await ctx.pool.fetchrow(
        """
        UPDATE users
        SET pregnancy_edd = $2,
            pregnancy_dating_basis = $3,
            pregnancy_scan_date = COALESCE($4, pregnancy_scan_date),
            pregnancy_scan_corrected_at = COALESCE($5, pregnancy_scan_corrected_at)
        WHERE id = $1
        RETURNING id
        """,
        ctx.user.id,
        edd_val,
        args.dating_basis,
        scan_date,
        scan_corrected_at,
    )

    weeks, days = gestational_age(edd_val)
    ga_str = f"{weeks}w{days}d"

    from dataclasses import replace as _replace

    ctx.user = _replace(
        ctx.user,
        pregnancy_edd=edd_val,
        pregnancy_dating_basis=args.dating_basis,
        pregnancy_scan_date=(
            scan_date if scan_date is not None else ctx.user.pregnancy_scan_date
        ),
        pregnancy_scan_corrected_at=(
            scan_corrected_at
            if scan_corrected_at is not None
            else ctx.user.pregnancy_scan_corrected_at
        ),
    )

    result = CorrectPregnancyEddOutput(ok=True, edd=args.edd, gestational_age=ga_str)
    await _log_tool_call(ctx, "correct_pregnancy_edd", args, started, result)
    return result


async def end_pregnancy(
    ctx: TurnContext, args: "EndPregnancyInput"
) -> "EndPregnancyOutput":
    """Close the active pregnancy. Errors if no active pregnancy or already ended."""
    from datetime import datetime as _datetime

    started = _start()

    row = await ctx.pool.fetchrow(
        "SELECT id, pregnancy_edd, pregnancy_ended_at, pregnancy_outcome FROM users WHERE id = $1 FOR UPDATE",
        ctx.user.id,
    )
    if row["pregnancy_edd"] is None:
        raise ToolCallRejected(
            {
                "error": "no_active_pregnancy",
                "reason": "There is no pregnancy to end — EDD has never been set.",
            }
        )
    if row["pregnancy_ended_at"] is not None:
        raise ToolCallRejected(
            {
                "error": "pregnancy_already_ended",
                "reason": (
                    f"pregnancy already ended on {row['pregnancy_ended_at'].isoformat()}"
                ),
            }
        )

    if args.ended_at:
        ended_at = _datetime.fromisoformat(args.ended_at)
    else:
        ended_at = _datetime.now(UTC)

    await ctx.pool.fetchrow(
        """
        UPDATE users
        SET pregnancy_ended_at = $2,
            pregnancy_outcome = $3
        WHERE id = $1
        RETURNING id
        """,
        ctx.user.id,
        ended_at,
        args.outcome,
    )

    from dataclasses import replace as _replace

    ctx.user = _replace(
        ctx.user,
        pregnancy_ended_at=ended_at,
        pregnancy_outcome=args.outcome,
    )

    result = EndPregnancyOutput(
        ok=True,
        outcome=args.outcome,
        ended_at=ended_at.isoformat(),
    )
    await _log_tool_call(ctx, "end_pregnancy", args, started, result)
    return result


def _check_edd_window(edd: "date") -> None:
    """Guard: reject EDD more than 1 year in the future (data-error guard)."""
    from datetime import date as _date, timedelta

    if edd > _date.today() + timedelta(days=365):
        raise ToolCallRejected(
            {
                "error": "edd_too_far_future",
                "reason": (
                    f"EDD {edd.isoformat()} is more than 1 year in the future. "
                    "This looks like a data error — please check the date."
                ),
            }
        )


async def set_topic_status(
    ctx: TurnContext, args: SetTopicStatusInput
) -> SetTopicStatusOutput:
    """Upsert the bot-authored status row for this topic+scope (§7).

    First-line write-scope check; XOR validation on scope/user_id/dyad_id;
    upsert preserves last_updated_by_bot_id (NOT NULL per migrations/0022:18).
    Returns updated_at = row['last_updated_at'].isoformat() on the output.
    """
    _err = check_write_scope(ctx)
    if _err is not None:
        raise ToolCallRejected({"error": _err})
    started = _start()
    if args.scope == "user":
        if args.user_id is None:
            raise ToolCallRejected(
                {"error": "set_topic_status: scope=user requires user_id"}
            )
        row = await ctx.pool.fetchrow(
            """
            INSERT INTO topic_status (topic_id, user_id, headline, body, last_updated_by_bot_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (topic_id, user_id) WHERE user_id IS NOT NULL
            DO UPDATE SET headline = EXCLUDED.headline,
                          body = EXCLUDED.body,
                          last_updated_at = now(),
                          last_updated_by_bot_id = EXCLUDED.last_updated_by_bot_id
            RETURNING id, headline, body, last_updated_at
            """,
            ctx.primary_topic_id,
            args.user_id,
            args.headline,
            args.body,
            ctx.bot_id,
        )
    else:
        if ctx.dyad_id is None:
            raise ToolCallRejected(
                {"error": "set_topic_status: scope=dyad requires ctx.dyad_id"}
            )
        row = await ctx.pool.fetchrow(
            """
            INSERT INTO topic_status (topic_id, dyad_id, headline, body, last_updated_by_bot_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (topic_id, dyad_id) WHERE dyad_id IS NOT NULL
            DO UPDATE SET headline = EXCLUDED.headline,
                          body = EXCLUDED.body,
                          last_updated_at = now(),
                          last_updated_by_bot_id = EXCLUDED.last_updated_by_bot_id
            RETURNING id, headline, body, last_updated_at
            """,
            ctx.primary_topic_id,
            ctx.dyad_id,
            args.headline,
            args.body,
            ctx.bot_id,
        )
    result = SetTopicStatusOutput(
        status_id=row["id"],
        headline=row["headline"],
        body=row["body"],
        updated_at=(
            row["last_updated_at"].isoformat() if row["last_updated_at"] else None
        ),
    )
    await _log_tool_call(ctx, "set_topic_status", args, started, result)
    return result


# ── Hector fitness write tools ─────────────────────────────────────────────


async def create_commitment(
    ctx: TurnContext, args: "CreateCommitmentInput"
) -> "CreateCommitmentOutput":
    """Create a new fitness commitment for the current user/topic/bot."""
    from datetime import date as _date, datetime as _datetime

    started = _start()

    # Scope guard: only Hector can create commitments for the fitness topic
    _check_hector_scope(ctx)

    sd = _date.fromisoformat(args.start_date) if args.start_date else _date.today()
    ed = _date.fromisoformat(args.end_date) if args.end_date else None
    sr = (
        args.schedule_rule.model_dump(exclude_none=True)
        if args.schedule_rule
        else {}
    )
    dow = args.days_of_week or []

    row = await ctx.pool.fetchrow(
        """
        INSERT INTO mediator.commitments
          (user_id, topic_id, bot_id, label, kind, cadence,
           days_of_week, target_count, start_date, end_date,
           schedule_rule, pressure_style)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
        RETURNING id, label, cadence, created_at
        """,
        ctx.user.id,
        ctx.primary_topic_id,
        ctx.bot_id,
        args.label,
        args.kind,
        args.cadence,
        dow,
        args.target_count,
        sd,
        ed,
        sr,
        args.pressure_style,
    )

    result = CreateCommitmentOutput(
        commitment_id=str(row["id"]),
        label=row["label"],
        cadence=row["cadence"],
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
    )
    await _ensure_commitment_checkin_task(ctx, args, row)
    await _log_tool_call(ctx, "create_commitment", args, started, result)
    return result


async def update_commitment(
    ctx: TurnContext, args: "UpdateCommitmentInput"
) -> "UpdateCommitmentOutput":
    """Update an existing commitment with partial field changes."""
    started = _start()
    _check_hector_scope(ctx)

    from datetime import date as _date  # noqa: PLC0415

    from app.services.tools.common import parse_required_uuid_field  # noqa: PLC0415

    _validated_cid = parse_required_uuid_field(
        args.commitment_id,
        field_name="commitment_id",
        tool_name="update_commitment",
    )

    updates: list[str] = []
    params: list[Any] = []
    param_idx = 1

    if args.label is not None:
        updates.append(f"label = ${param_idx}")
        params.append(args.label)
        param_idx += 1
    if args.kind is not None:
        updates.append(f"kind = ${param_idx}")
        params.append(args.kind)
        param_idx += 1
    if args.cadence is not None:
        updates.append(f"cadence = ${param_idx}")
        params.append(args.cadence)
        param_idx += 1
    if args.days_of_week is not None:
        updates.append(f"days_of_week = ${param_idx}")
        params.append(args.days_of_week)
        param_idx += 1
    if args.target_count is not None:
        updates.append(f"target_count = ${param_idx}")
        params.append(args.target_count)
        param_idx += 1
    if args.start_date is not None:
        updates.append(f"start_date = ${param_idx}")
        params.append(_date.fromisoformat(args.start_date))
        param_idx += 1
    if args.end_date is not None:
        updates.append(f"end_date = ${param_idx}")
        params.append(_date.fromisoformat(args.end_date))
        param_idx += 1
    if args.schedule_rule is not None:
        updates.append(f"schedule_rule = ${param_idx}::jsonb")
        params.append(args.schedule_rule.model_dump(exclude_none=True))
        param_idx += 1
    if args.pressure_style is not None:
        updates.append(f"pressure_style = ${param_idx}")
        params.append(args.pressure_style)
        param_idx += 1

    if not updates:
        raise ToolCallRejected(
            {"error": "update_commitment: no fields to update"}
        )

    updates.append("updated_at = now()")
    params.extend([ctx.user.id, ctx.primary_topic_id, ctx.bot_id, str(_validated_cid)])

    row = await ctx.pool.fetchrow(
        f"""
        UPDATE mediator.commitments
        SET {', '.join(updates)}
        WHERE user_id = ${param_idx}
          AND topic_id = ${param_idx + 1}
          AND bot_id = ${param_idx + 2}
          AND id = ${param_idx + 3}::uuid
        RETURNING id, updated_at
        """,
        *params,
    )

    if row is None:
        raise ToolCallRejected(
            {
                "error": "update_commitment: commitment not found or access denied",
                "is_error": True,
                "error_code": "not_found",
                "field": "commitment_id",
                "retryable": True,
                "correction_hint": (
                    "Call list_commitments to find existing commitments; "
                    "if none match, call create_commitment and use the returned commitment_id."
                ),
                "failure_class": "tool_validation_recoverable",
            }
        )

    result = UpdateCommitmentOutput(
        commitment_id=str(row["id"]),
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
    )
    await _log_tool_call(ctx, "update_commitment", args, started, result)
    return result


async def close_commitment(
    ctx: TurnContext, args: "CloseCommitmentInput"
) -> "CloseCommitmentOutput":
    """Close a commitment (pause, complete, or drop)."""
    started = _start()
    _check_hector_scope(ctx)

    from app.services.tools.common import parse_required_uuid_field  # noqa: PLC0415

    _validated_cid = parse_required_uuid_field(
        args.commitment_id,
        field_name="commitment_id",
        tool_name="close_commitment",
    )

    row = await ctx.pool.fetchrow(
        """
        UPDATE mediator.commitments
        SET status = $5, updated_at = now()
        WHERE user_id = $1
          AND topic_id = $2
          AND bot_id = $3
          AND id = $4::uuid
        RETURNING id, status, updated_at
        """,
        ctx.user.id,
        ctx.primary_topic_id,
        ctx.bot_id,
        str(_validated_cid),
        args.status,
    )

    if row is None:
        raise ToolCallRejected(
            {
                "error": "close_commitment: commitment not found or access denied",
                "is_error": True,
                "error_code": "not_found",
                "field": "commitment_id",
                "retryable": True,
                "correction_hint": (
                    "Call list_commitments to find existing commitments; "
                    "if none match, call create_commitment and use the returned commitment_id."
                ),
                "failure_class": "tool_validation_recoverable",
            }
        )

    result = CloseCommitmentOutput(
        commitment_id=str(row["id"]),
        status=row["status"],
        closed_at=row["updated_at"].isoformat() if row["updated_at"] else None,
    )
    await _cancel_commitment_checkin_tasks(ctx, args.commitment_id)
    await _log_tool_call(ctx, "close_commitment", args, started, result)
    return result


async def log_event(
    ctx: TurnContext, args: "LogEventInput"
) -> "LogEventOutput":
    """Log a fitness event (adherence outcome or measurement)."""
    from datetime import datetime as _datetime

    started = _start()
    _check_hector_scope(ctx)

    from app.services.tools.common import parse_optional_uuid_field  # noqa: PLC0415

    _validated_cid = parse_optional_uuid_field(
        args.commitment_id,
        field_name="commitment_id",
        tool_name="log_event",
    )

    # Existence / access check: a valid UUID must reference an accessible commitment.
    if _validated_cid is not None:
        exists = await ctx.pool.fetchrow(
            "SELECT id FROM mediator.commitments "
            "WHERE id = $1::uuid AND user_id = $2 AND topic_id = $3 AND bot_id = $4",
            str(_validated_cid),
            ctx.user.id,
            ctx.primary_topic_id,
            ctx.bot_id,
        )
        if exists is None:
            raise ToolCallRejected(
                {
                    "error": "commitment_not_found",
                    "is_error": True,
                    "error_code": "not_found",
                    "field": "commitment_id",
                    "retryable": True,
                    "correction_hint": (
                        "Call list_commitments to find existing commitments; "
                        "if none match, call create_commitment and use the returned commitment_id."
                    ),
                    "failure_class": "tool_validation_recoverable",
                }
            )

    obs = (
        _datetime.fromisoformat(args.observed_at.replace("Z", "+00:00"))
        if args.observed_at
        else _datetime.now(UTC)
    )
    if obs.tzinfo is None:
        obs = obs.replace(tzinfo=UTC)

    source_ids = list(args.source_message_ids) if args.source_message_ids else []

    row = await ctx.pool.fetchrow(
        """
        INSERT INTO mediator.events
          (commitment_id, user_id, topic_id, bot_id, metric_key,
           adherence_status, value_numeric, value_text, unit,
           observed_at, note, source_message_ids)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::uuid[])
        RETURNING id, commitment_id, metric_key, adherence_status, observed_at
        """,
        str(_validated_cid) if _validated_cid is not None else None,
        ctx.user.id,
        ctx.primary_topic_id,
        ctx.bot_id,
        args.metric_key,
        args.adherence_status,
        args.value_numeric,
        args.value_text,
        args.unit,
        obs,
        args.note,
        source_ids,
    )

    result = LogEventOutput(
        event_id=str(row["id"]),
        commitment_id=str(row["commitment_id"]) if row["commitment_id"] else None,
        metric_key=row["metric_key"],
        adherence_status=row["adherence_status"],
        observed_at=row["observed_at"].isoformat() if row["observed_at"] else None,
    )
    await _log_tool_call(ctx, "log_event", args, started, result)
    return result


_COMMITMENT_BOT_IDS: frozenset[str] = frozenset({"hector", "habits"})
_COMMITMENT_TOPIC_SLUGS: frozenset[str] = frozenset({"fitness", "habits"})


def _check_hector_scope(ctx: TurnContext) -> None:
    """Enforce that only commitment-tracking bots can call these tools.

    Rejects if any scope value (bot_id, primary_topic_id, user.id) is None,
    or if the (bot_id, topic_slug) pair is not in the commitment-tracking
    set. Today: Hector on `fitness`, Habits on `habits`.
    """
    if ctx.bot_id is None:
        raise ToolCallRejected(
            {
                "error": "scope_denied: missing bot_id",
                "reason": "Commitment/event tools require ctx.bot_id (got None)",
            }
        )
    if ctx.primary_topic_id is None:
        raise ToolCallRejected(
            {
                "error": "scope_denied: missing topic_id",
                "reason": "Commitment/event tools require ctx.primary_topic_id (got None)",
            }
        )
    if ctx.user.id is None:
        raise ToolCallRejected(
            {
                "error": "scope_denied: missing user_id",
                "reason": "Commitment/event tools require ctx.user.id (got None)",
            }
        )
    if ctx.bot_id not in _COMMITMENT_BOT_IDS:
        raise ToolCallRejected(
            {
                "error": "scope_denied: wrong bot",
                "reason": (
                    f"Commitment/event tools are restricted to "
                    f"{sorted(_COMMITMENT_BOT_IDS)}, got bot_id={ctx.bot_id!r}"
                ),
            }
        )
    if ctx.primary_topic_slug not in _COMMITMENT_TOPIC_SLUGS:
        raise ToolCallRejected(
            {
                "error": "scope_denied: wrong topic",
                "reason": (
                    f"Commitment/event tools require a commitment topic "
                    f"({sorted(_COMMITMENT_TOPIC_SLUGS)}), "
                    f"got primary_topic_slug={ctx.primary_topic_slug!r}"
                ),
            }
        )


async def _ensure_commitment_checkin_task(
    ctx: TurnContext,
    args: "CreateCommitmentInput",
    row: Any,
) -> None:
    """Create a recurring internal check-in for a new commitment.

    The scheduled turn checks adherence first and stays silent when the slot
    is already handled, so the reminder is conditional rather than a blind nag.
    """

    scheduled_for, recurrence = _commitment_checkin_schedule(ctx, args)
    if scheduled_for is None or recurrence is None:
        return

    commitment_id = str(row["id"])
    context = {
        "task_id": str(uuid4()),
        "kind": "commitment_checkin",
        "commitment_id": commitment_id,
        "commitment_label": row["label"],
        "recurrence": recurrence,
        "brief": (
            f"Check commitment '{row['label']}'. First read "
            "the adherence board with get_adherence and recent events if "
            "needed. If the relevant slot is already done, missed, or excused, "
            "stay silent unless a short acknowledgement is clearly useful. If "
            "it is still unknown after the intended window, ask one low-key "
            "question and log the result if the user answers. If the commitment "
            "is no longer active, cancel this current scheduled task."
        ),
    }
    try:
        await ctx.pool.fetchrow(
            """
            INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
            VALUES ($1, 'scheduled_task', $2, $3::jsonb, 'pending', $4, $5)
            RETURNING id AS job_id, scheduled_for, context
            """,
            ctx.user.id,
            scheduled_for,
            context,
            ctx.bot_id,
            ctx.primary_topic_id,
        )
    except Exception:
        logger.exception(
            "failed to schedule commitment check-in",
            extra=obs_fields(ctx),
        )


def _commitment_checkin_schedule(
    ctx: TurnContext,
    args: "CreateCommitmentInput",
) -> tuple[datetime | None, dict[str, Any] | None]:
    timezone = _commitment_timezone(ctx, args)
    now_local = datetime.now(UTC).astimezone(timezone)
    start = date.fromisoformat(args.start_date) if args.start_date else now_local.date()
    end = date.fromisoformat(args.end_date) if args.end_date else None

    if args.cadence == "daily":
        weekdays = None
        local_time = datetime.min.time().replace(hour=20, minute=30)
        recurrence: dict[str, Any] = {"type": "daily", "interval": 1}
    elif args.cadence == "weekdays":
        weekdays = [0, 1, 2, 3, 4]
        local_time = datetime.min.time().replace(hour=20, minute=30)
        recurrence = {"type": "weekly", "interval": 1, "weekdays": weekdays}
    elif args.cadence == "custom_days":
        weekdays = list(args.days_of_week or [])
        local_time = datetime.min.time().replace(hour=20, minute=30)
        recurrence = {"type": "weekly", "interval": 1, "weekdays": weekdays}
    elif args.cadence in {"weekly_count", "custom"}:
        weekdays = [6]
        local_time = datetime.min.time().replace(hour=19, minute=0)
        recurrence = {"type": "weekly", "interval": 1, "weekdays": weekdays}
    else:
        return None, None

    if end is not None:
        recurrence["until"] = datetime.combine(
            end,
            datetime.max.time().replace(microsecond=0),
            tzinfo=timezone,
        ).astimezone(UTC).isoformat()

    first = _next_local_commitment_checkin(
        now_local=now_local,
        start=start,
        local_time=local_time,
        weekdays=weekdays,
    )
    if end is not None and first.date() > end:
        return None, None
    return first.astimezone(UTC), recurrence


def _commitment_timezone(ctx: TurnContext, args: "CreateCommitmentInput") -> ZoneInfo:
    timezone_name = None
    if args.schedule_rule is not None:
        timezone_name = args.schedule_rule.timezone
    timezone_name = timezone_name or getattr(ctx.user, "timezone", None) or "UTC"
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _next_local_commitment_checkin(
    *,
    now_local: datetime,
    start: date,
    local_time: Any,
    weekdays: list[int] | None,
) -> datetime:
    candidate_date = max(now_local.date(), start)
    for offset in range(0, 15):
        day = candidate_date + timedelta(days=offset)
        if weekdays is not None and day.weekday() not in weekdays:
            continue
        candidate = datetime.combine(day, local_time, tzinfo=now_local.tzinfo)
        if candidate > now_local:
            return candidate
    return datetime.combine(
        candidate_date + timedelta(days=1),
        local_time,
        tzinfo=now_local.tzinfo,
    )


async def _cancel_commitment_checkin_tasks(
    ctx: TurnContext,
    commitment_id: str,
) -> None:
    try:
        await ctx.pool.execute(
            """
            UPDATE scheduled_jobs
            SET status = 'cancelled',
                cancellation_reason = 'commitment_closed',
                updated_at = now()
            WHERE user_id = $1
              AND bot_id = $2
              AND topic_id = $3
              AND job_type = 'scheduled_task'
              AND status = 'pending'
              AND context->>'kind' = 'commitment_checkin'
              AND context->>'commitment_id' = $4
            """,
            ctx.user.id,
            ctx.bot_id,
            ctx.primary_topic_id,
            str(commitment_id),
        )
    except Exception:
        logger.exception(
            "failed to cancel commitment check-in tasks",
            extra=obs_fields(ctx),
        )


# ── Conversation-plan write tools ──────────────────────────────────────────


async def create_conversation_plan(
    ctx: TurnContext, args: CreateConversationPlanInput
) -> CreateConversationPlanOutput:
    """Create a new conversation with agenda items from a markdown plan."""
    try:
        from app.bots.registry import get_bot_spec, primary_topic_id_for  # noqa: PLC0415

        bot_spec = get_bot_spec(ctx.bot_id)
        topic_id = await primary_topic_id_for(ctx.pool, bot_spec)
    except Exception:
        raise ToolCallRejected(
            {"error": "topic resolution failed; refusing context-less conversation"}
        )

    agenda = markdown_to_agenda(args.plan_markdown, args.prep_summary)
    mode = "steered" if (args.prep_summary or "").strip() else "open"
    item_uuid_by_id = {item.id: uuid4() for item in agenda.items}
    first_uuid = item_uuid_by_id[agenda.first_item_id]
    started = _start()

    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            conv_row = await conn.fetchrow(
                """
                INSERT INTO mediator.conversations
                    (user_id, bot_id, topic_id, status, mode, prep_summary)
                VALUES ($1, $2, $3, 'ready', $4, $5)
                RETURNING id
                """,
                ctx.user.id,
                ctx.bot_id,
                topic_id,
                mode,
                args.prep_summary,
            )
            conv_id = conv_row["id"]

            for item in agenda.items:
                item_uuid = item_uuid_by_id[item.id]
                next_uuids = [item_uuid_by_id[ref] for ref in item.next_item_ids]
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_items
                        (id, conversation_id, theme_id, kind, title, intent, ask,
                         done_when, next_item_ids, priority, speaker_scope,
                         coverage_evidence_required, order_hint)
                    VALUES ($1, $2, $3, 'planned', $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """,
                    item_uuid,
                    conv_id,
                    None,  # theme_id
                    item.title,
                    item.intent,
                    item.ask,
                    item.done_when,
                    next_uuids,
                    item.priority,
                    item.speaker_scope,
                    item.coverage_evidence_required,
                    item.order_hint,
                )

            await conn.execute(
                "UPDATE mediator.conversations SET current_item_id=$1 WHERE id=$2",
                first_uuid,
                conv_id,
            )

    plan_items = [
        PlanItem(
            id=item_uuid_by_id[item.id],
            title=item.title,
            priority=item.priority,
            order_hint=item.order_hint,
        )
        for item in agenda.items
    ]
    result = CreateConversationPlanOutput(
        conversation_id=conv_id,
        status="ready",
        items=plan_items,
        display_text=agenda_to_display(agenda.items),
    )
    await _log_tool_call(ctx, "create_conversation_plan", args, started, result)
    return result


async def update_conversation_plan(
    ctx: TurnContext, args: UpdateConversationPlanInput
) -> UpdateConversationPlanOutput:
    """Replace the agenda items on an existing prepping/ready conversation."""
    started = _start()

    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            conv_row = await conn.fetchrow(
                """
                SELECT id, status, mode
                FROM mediator.conversations
                WHERE id=$1 AND user_id=$2
                FOR UPDATE
                """,
                args.conversation_id,
                ctx.user.id,
            )
            if conv_row is None:
                raise ToolCallRejected({"error": "not found or not owned"})

            conv_status = conv_row["status"]
            if conv_status not in ("prepping", "preparing", "ready"):
                raise ToolCallRejected(
                    {"error": f"cannot edit agenda while status={conv_status!r}"}
                )

            await conn.execute(
                """
                DELETE FROM mediator.conversation_items
                WHERE conversation_id=$1 AND kind='planned'
                """,
                args.conversation_id,
            )

            agenda = markdown_to_agenda(args.plan_markdown, args.prep_summary)
            item_uuid_by_id = {item.id: uuid4() for item in agenda.items}
            new_first_uuid = item_uuid_by_id[agenda.first_item_id]

            for item in agenda.items:
                item_uuid = item_uuid_by_id[item.id]
                next_uuids = [item_uuid_by_id[ref] for ref in item.next_item_ids]
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_items
                        (id, conversation_id, theme_id, kind, title, intent, ask,
                         done_when, next_item_ids, priority, speaker_scope,
                         coverage_evidence_required, order_hint)
                    VALUES ($1, $2, $3, 'planned', $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """,
                    item_uuid,
                    args.conversation_id,
                    None,  # theme_id
                    item.title,
                    item.intent,
                    item.ask,
                    item.done_when,
                    next_uuids,
                    item.priority,
                    item.speaker_scope,
                    item.coverage_evidence_required,
                    item.order_hint,
                )

            if args.prep_summary is not None:
                new_mode = "steered" if (args.prep_summary or "").strip() else "open"
                await conn.execute(
                    """
                    UPDATE mediator.conversations
                    SET current_item_id=$1, prep_summary=$2, mode=$3
                    WHERE id=$4
                    """,
                    new_first_uuid,
                    args.prep_summary,
                    new_mode,
                    args.conversation_id,
                )
            else:
                await conn.execute(
                    "UPDATE mediator.conversations SET current_item_id=$1 WHERE id=$2",
                    new_first_uuid,
                    args.conversation_id,
                )

    plan_items = [
        PlanItem(
            id=item_uuid_by_id[item.id],
            title=item.title,
            priority=item.priority,
            order_hint=item.order_hint,
        )
        for item in agenda.items
    ]
    result = UpdateConversationPlanOutput(
        conversation_id=args.conversation_id,
        status=conv_status,
        items=plan_items,
        display_text=agenda_to_display(agenda.items),
    )
    await _log_tool_call(ctx, "update_conversation_plan", args, started, result)
    return result
