"""Write tools for the agentic loop."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.services.checkins import schedule_checkin_record
from app.services.cross_thread_privacy import normalize_sharing_default, raw_message_visibility
from app.services.crypto import encrypt_value
from app.config import get_settings
from app.services.vision import explain_stored_image
from app.services.messaging import send_outbound, _append_turn_reasoning, _call_oob_hook
from app.services import discord, scoring
from app.services.templates import TemplateCall
from app.services.turn_context import TurnContext
from app.services.scheduled_task_recurrence import normalize_recurrence
from app.services.tools.common import current_scheduled_task
from tool_schemas import (
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
    CancelScheduledCheckinInput,
    CancelScheduledCheckinOutput,
    CreateBridgeCandidateInput,
    CreateBridgeCandidateOutput,
    CreateThemeInput,
    CreateThemeOutput,
    DeleteOutboundMessageInput,
    DeleteOutboundMessageOutput,
    EditOutboundMessageInput,
    EditOutboundMessageOutput,
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
    ReactToMessageInput,
    ReactToMessageOutput,
    ReviseDistillationInput,
    ReviseDistillationOutput,
    ScheduleCheckinInput,
    ScheduleCheckinOutput,
    ScheduleDelay,
    ScheduleTaskInput,
    ScheduleTaskOutput,
    ScheduledTaskRow,
    SendBridgeCandidateInput,
    SendBridgeCandidateOutput,
    SupersedeMemoryInput,
    SupersedeMemoryOutput,
    UpdateBridgeCandidateInput,
    UpdateBridgeCandidateOutput,
    UpdateDistillationInput,
    UpdateDistillationOutput,
    UpdateMemoryInput,
    UpdateMemoryOutput,
    UpdateCrossThreadSharingDefaultInput,
    UpdateCrossThreadSharingDefaultOutput,
    UpdateOOBInput,
    UpdateOOBOutput,
    UpdateObservationInput,
    UpdateObservationOutput,
    UpdateScheduledTaskInput,
    UpdateScheduledTaskOutput,
    UpdateThemeInput,
    UpdateThemeOutput,
    UpdateUserStyleNotesInput,
    UpdateUserStyleNotesOutput,
    UpdateWatchItemInput,
    UpdateWatchItemOutput,
    CancelScheduledTaskInput,
    CancelScheduledTaskOutput,
)

logger = logging.getLogger(__name__)

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
    duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
    await ctx.pool.execute(
        """
        INSERT INTO tool_calls (turn_id, tool_name, arguments, result, called_at, duration_ms)
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
        """,
        ctx.turn_id,
        name,
        _jsonb_payload(args),
        _jsonb_payload(result),
        started_at,
        duration_ms,
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
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status)
        VALUES ($1, $2, $3, $4::jsonb, 'pending')
        RETURNING id, scheduled_for
        """,
        user_id,
        job_type,
        scheduled_for,
        {context_key: str(context_id)},
    )


async def update_user_style_notes(ctx: TurnContext, args: UpdateUserStyleNotesInput) -> UpdateUserStyleNotesOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        "UPDATE users SET style_notes=$1 WHERE id=$2 RETURNING id AS user_id, now() AS updated_at",
        args.notes,
        args.user_id,
    )
    result = UpdateUserStyleNotesOutput(user_id=row["user_id"], updated_at=row["updated_at"])
    await _log_tool_call(ctx, "update_user_style_notes", args, started, result)
    return result


async def update_cross_thread_sharing_default(
    ctx: TurnContext,
    args: UpdateCrossThreadSharingDefaultInput,
) -> UpdateCrossThreadSharingDefaultOutput:
    started = _start()
    if args.user_id != ctx.user.id:
        result = {
            "error": "sharing_default_rejected",
            "reason": "can only update the current user's sharing default",
        }
        await _log_tool_call(ctx, "update_cross_thread_sharing_default", args, started, result)
        raise ToolCallRejected(result)
    row = await ctx.pool.fetchrow(
        """
        UPDATE users
        SET cross_thread_sharing_default=$2
        WHERE id=$1
        RETURNING id AS user_id, cross_thread_sharing_default, now() AS updated_at
        """,
        args.user_id,
        args.default.value,
    )
    result = UpdateCrossThreadSharingDefaultOutput(
        user_id=row["user_id"],
        default=row["cross_thread_sharing_default"],
        updated_at=row["updated_at"],
    )
    await _append_turn_reasoning(
        ctx.pool,
        ctx.turn_id,
        f"Cross-thread sharing default set for user_id={args.user_id}: {args.default.value}. reason={args.reason}",
    )
    await _log_tool_call(ctx, "update_cross_thread_sharing_default", args, started, result)
    return result


async def create_bridge_candidate(
    ctx: TurnContext,
    args: CreateBridgeCandidateInput,
) -> CreateBridgeCandidateOutput:
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
    await _require_existing_source_messages(ctx, args.source_message_ids, args.source_user_id)
    await _require_existing_ids(ctx, "memories", args.related_memory_ids)
    await _require_existing_ids(ctx, "observations", args.related_observation_ids)

    if args.partner_path == BridgeCandidatePartnerPath.do_not_bridge:
        status = BridgeCandidateStatus.declined
    elif args.status == BridgeCandidateStatus.blocked:
        status = BridgeCandidateStatus.blocked
    else:
        source_default = await _sharing_default_for_user(ctx, args.source_user_id)
        low_or_medium = args.sensitivity in {
            BridgeCandidateSensitivity.low,
            BridgeCandidateSensitivity.medium,
        }
        status = BridgeCandidateStatus.ready if source_default == "opt_in" and low_or_medium else BridgeCandidateStatus.pending
    resolved_at_sql = "now()" if status.value in _BRIDGE_RESOLVED_STATUSES else "NULL"
    row = await ctx.pool.fetchrow(
        f"""
        INSERT INTO bridge_candidates (
            source_user_id, target_user_id, kind, status, sensitivity, partner_path,
            source_message_ids, related_memory_ids, related_observation_ids,
            internal_note, shareable_summary, resolved_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8::uuid[], $9::uuid[], $10, $11, {resolved_at_sql})
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
    )
    result = CreateBridgeCandidateOutput(candidate=_bridge_candidate(row))
    await _log_tool_call(ctx, "create_bridge_candidate", args, started, result)
    return result


async def update_bridge_candidate(
    ctx: TurnContext,
    args: UpdateBridgeCandidateInput,
) -> UpdateBridgeCandidateOutput:
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
            or
            args.kind is not None
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
    if args.partner_path is not None and existing["status"] in _BRIDGE_RESOLVED_STATUSES:
        result = {
            "error": "bridge_candidate_partner_path_locked",
            "reason": "partner_path cannot be changed after a bridge candidate reaches a terminal status",
        }
        await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
        raise ToolCallRejected(result)
    if args.partner_path == BridgeCandidatePartnerPath.do_not_bridge:
        if status_update is not None and status_update != BridgeCandidateStatus.declined:
            result = {
                "error": "bridge_candidate_status_rejected",
                "reason": "do_not_bridge candidates must be marked declined",
            }
            await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
            raise ToolCallRejected(result)
        status_update = BridgeCandidateStatus.declined
    if args.source_message_ids is not None:
        await _require_existing_source_messages(ctx, args.source_message_ids, existing["source_user_id"])
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
    result = UpdateBridgeCandidateOutput(candidate=_bridge_candidate_for_context(ctx, row))
    await _log_tool_call(ctx, "update_bridge_candidate", args, started, result)
    return result


async def send_bridge_candidate(
    ctx: TurnContext,
    args: SendBridgeCandidateInput,
) -> SendBridgeCandidateOutput:
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
    verdict = await _call_oob_hook(ctx.pool, content, target.id, protected_owner_ids)
    if verdict["verdict"] in {"block", "rewrite"}:
        note = _append_note(existing["internal_note"], f"OOB {verdict['verdict']}: {verdict.get('reason', '')}")
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


def _bridge_users_in_current_dyad(ctx: TurnContext, source_user_id: Any, target_user_id: Any) -> bool:
    dyad = {ctx.user.id, ctx.partner.id}
    return source_user_id in dyad and target_user_id in dyad and source_user_id != target_user_id


async def _sharing_default_for_user(ctx: TurnContext, user_id: Any) -> str:
    row = await ctx.pool.fetchrow("SELECT cross_thread_sharing_default FROM users WHERE id=$1", user_id)
    if row is not None:
        return normalize_sharing_default(row["cross_thread_sharing_default"])
    for user in (ctx.user, ctx.partner):
        if user.id == user_id:
            return normalize_sharing_default(user.cross_thread_sharing_default)
    return "unset"


async def _require_existing_source_messages(ctx: TurnContext, message_ids: list[Any], source_user_id: Any) -> None:
    rows = await ctx.pool.fetch(
        """
        SELECT id
        FROM messages
        WHERE id = ANY($1::uuid[])
          AND deleted_at IS NULL
          AND (sender_id=$2 OR recipient_id=$2)
        """,
        message_ids,
        source_user_id,
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
    rows = await ctx.pool.fetch(f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", ids)
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
        invalid_sources = [user_id for user_id in source_user_ids if user_id not in dyad_ids]
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
                  AND (
                    sender_id = ANY($2::uuid[])
                    OR recipient_id = ANY($2::uuid[])
                  )
                """,
                ids,
                list(dyad_ids),
            )
        else:
            rows = await ctx.pool.fetch(f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", ids)
        found = {row["id"] for row in rows}
        missing = [row_id for row_id in ids if row_id not in found]
        if missing:
            raise ToolCallRejected(
                {
                    "error": "distillation_related_ids_not_found",
                    "reason": f"{table} ids were not found or not visible: {missing}",
                }
            )


def _default_supporting_message_ids(ctx: TurnContext, message_ids: list[Any]) -> list[Any]:
    return message_ids or list(ctx.triggering_message_ids)


async def _fetch_bridge_candidate_row(ctx: TurnContext, candidate_id: Any) -> Any | None:
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
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO memories (about_user_id, content, content_encrypted, related_theme_ids)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        args.about_user_id,
        args.content,
        encrypt_value(args.content),
        args.related_theme_ids,
    )
    result = AddMemoryOutput(id=row["id"])
    await _log_tool_call(ctx, "add_memory", args, started, result)
    return result


async def update_memory(ctx: TurnContext, args: UpdateMemoryInput) -> UpdateMemoryOutput:
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
    row = await ctx.pool.fetchrow(f"UPDATE memories SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    result = UpdateMemoryOutput(id=row["id"])
    await _log_tool_call(ctx, "update_memory", args, started, result)
    return result


async def supersede_memory(ctx: TurnContext, args: SupersedeMemoryInput) -> SupersedeMemoryOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        WITH old AS (
            UPDATE memories SET status='superseded'
            WHERE id=$1
            RETURNING id, about_user_id
        )
        INSERT INTO memories (about_user_id, content, content_encrypted, related_theme_ids, supersedes_memory_id)
        SELECT about_user_id, $2, $3, $4, id FROM old
        RETURNING id AS new_id, $1::uuid AS old_id
        """,
        args.old_memory_id,
        args.new_content,
        encrypt_value(args.new_content),
        args.related_theme_ids,
    )
    result = SupersedeMemoryOutput(new_id=row["new_id"], old_id=row["old_id"])
    await _log_tool_call(ctx, "supersede_memory", args, started, result)
    return result


async def create_theme(ctx: TurnContext, args: CreateThemeInput) -> CreateThemeOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO themes (title, description, sentiment, health, last_reinforced_at)
        VALUES ($1, $2, $3, $4, now())
        RETURNING id
        """,
        args.title,
        args.description,
        args.sentiment.value,
        args.health.value,
    )
    result = CreateThemeOutput(id=row["id"])
    await _log_tool_call(ctx, "create_theme", args, started, result)
    return result


async def update_theme(ctx: TurnContext, args: UpdateThemeInput) -> UpdateThemeOutput:
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
    row = await ctx.pool.fetchrow(f"UPDATE themes SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    result = UpdateThemeOutput(id=row["id"])
    await _log_tool_call(ctx, "update_theme", args, started, result)
    return result


async def add_watch_item(ctx: TurnContext, args: AddWatchItemInput) -> AddWatchItemOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO watch_items (owner_user_id, content, due_at, related_theme_ids)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        args.owner_user_id,
        args.content,
        args.due_at,
        args.related_theme_ids,
    )
    if args.due_at is not None:
        await _schedule_context_job(
            ctx.pool,
            user_id=args.owner_user_id,
            job_type="watch_item_due",
            scheduled_for=args.due_at,
            context_key="watch_item_id",
            context_id=row["id"],
        )
    result = AddWatchItemOutput(id=row["id"])
    await _log_tool_call(ctx, "add_watch_item", args, started, result)
    return result


async def update_watch_item(ctx: TurnContext, args: UpdateWatchItemInput) -> UpdateWatchItemOutput:
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
    row = await ctx.pool.fetchrow(f"UPDATE watch_items SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    if args.due_at is not None:
        owner_user_id = await ctx.pool.fetchval("SELECT owner_user_id FROM watch_items WHERE id=$1", args.watch_item_id)
        await _schedule_context_job(
            ctx.pool,
            user_id=owner_user_id,
            job_type="watch_item_due",
            scheduled_for=args.due_at,
            context_key="watch_item_id",
            context_id=args.watch_item_id,
        )
    result = UpdateWatchItemOutput(id=row["id"])
    await _log_tool_call(ctx, "update_watch_item", args, started, result)
    return result


async def address_watch_item(ctx: TurnContext, args: AddressWatchItemInput) -> AddressWatchItemOutput:
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


async def log_observation(ctx: TurnContext, args: LogObservationInput) -> LogObservationOutput:
    started = _start()
    significance = args.significance
    supporting_message_ids = args.supporting_message_ids or ctx.triggering_message_ids
    logged_args = args.model_copy(update={"supporting_message_ids": supporting_message_ids})
    scoring_prompt_version = SCORING_PROMPT_VERSION
    if significance is None:
        significance, _reason, scoring_prompt_version = await scoring.score_observation(ctx.pool, content=args.content)
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO observations (
            content, content_encrypted, about_user_id, confidence, significance, scoring_prompt_version,
            related_theme_ids, supporting_message_ids, last_reinforced_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
        RETURNING id
        """,
        args.content,
        encrypt_value(args.content),
        args.about_user_id,
        args.confidence.value,
        significance,
        scoring_prompt_version,
        args.related_theme_ids,
        supporting_message_ids,
    )
    result = LogObservationOutput(id=row["id"])
    await _log_tool_call(ctx, "log_observation", logged_args, started, result)
    return result


async def update_observation(ctx: TurnContext, args: UpdateObservationInput) -> UpdateObservationOutput:
    started = _start()
    sets = ["last_reinforced_at=now()"]
    params: list[Any] = []
    for field in ("content", "confidence", "status", "related_theme_ids"):
        value = getattr(args, field)
        if value is not None:
            params.append(value.value if hasattr(value, "value") else value)
            sets.append(f"{field}=${len(params)}")
            if field == "content":
                params.append(encrypt_value(value))
                sets.append(f"content_encrypted=${len(params)}")
    params.append(args.observation_id)
    row = await ctx.pool.fetchrow(f"UPDATE observations SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    result = UpdateObservationOutput(id=row["id"])
    await _log_tool_call(ctx, "update_observation", args, started, result)
    return result


async def add_distillation(ctx: TurnContext, args: AddDistillationInput) -> AddDistillationOutput:
    started = _start()
    supporting_message_ids = _default_supporting_message_ids(ctx, args.supporting_message_ids)
    logged_args = args.model_copy(update={"supporting_message_ids": supporting_message_ids})
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
        INSERT INTO distillations (
            content, content_encrypted, confidence, sensitivity, visibility,
            shareable_summary, shareable_summary_encrypted, source_user_ids,
            related_memory_ids, related_observation_ids, related_theme_ids,
            supporting_message_ids, triggering_message_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid[], $9::uuid[], $10::uuid[], $11::uuid[], $12::uuid[], $13)
        RETURNING id
        """,
        args.content,
        encrypt_value(args.content),
        args.confidence.value,
        args.sensitivity.value,
        args.visibility.value,
        args.shareable_summary,
        encrypt_value(args.shareable_summary) if args.shareable_summary is not None else None,
        args.source_user_ids,
        args.related_memory_ids,
        args.related_observation_ids,
        args.related_theme_ids,
        supporting_message_ids,
        triggering_message_id,
    )
    result = AddDistillationOutput(id=row["id"])
    await _log_tool_call(ctx, "add_distillation", logged_args, started, result)
    return result


async def update_distillation(ctx: TurnContext, args: UpdateDistillationInput) -> UpdateDistillationOutput:
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
    await _log_tool_call(ctx, "update_distillation", args, started, result)
    return result


async def revise_distillation(ctx: TurnContext, args: ReviseDistillationInput) -> ReviseDistillationOutput:
    started = _start()
    supporting_message_ids = _default_supporting_message_ids(ctx, args.supporting_message_ids)
    logged_args = args.model_copy(update={"supporting_message_ids": supporting_message_ids})
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
        await _log_tool_call(ctx, "revise_distillation", logged_args, started, exc.result)
        raise
    triggering_message_id = args.triggering_message_id
    if triggering_message_id is None and supporting_message_ids:
        triggering_message_id = supporting_message_ids[0]
    row = await ctx.pool.fetchrow(
        """
        WITH old AS (
            SELECT id, revision_count
            FROM distillations
            WHERE id=$1 AND status='active'
        ),
        new AS (
            INSERT INTO distillations (
                content, content_encrypted, confidence, sensitivity, visibility,
                shareable_summary, shareable_summary_encrypted, source_user_ids,
                related_memory_ids, related_observation_ids, related_theme_ids,
                supporting_message_ids, triggering_message_id, supersedes_distillation_id,
                revision_note, revision_count
            )
            SELECT $2, $3, $4, $5, $6, $7, $8, $9::uuid[], $10::uuid[], $11::uuid[], $12::uuid[],
                   $13::uuid[], $14, old.id, $15, old.revision_count + 1
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
        encrypt_value(args.shareable_summary) if args.shareable_summary is not None else None,
        args.source_user_ids,
        args.related_memory_ids,
        args.related_observation_ids,
        args.related_theme_ids,
        supporting_message_ids,
        triggering_message_id,
        args.revision_note,
    )
    if row is None:
        result = {
            "error": "distillation_revision_rejected",
            "reason": "old_distillation_id was not found or is not active",
        }
        await _log_tool_call(ctx, "revise_distillation", logged_args, started, result)
        raise ToolCallRejected(result)
    result = ReviseDistillationOutput(new_id=row["new_id"], old_id=row["old_id"])
    await _log_tool_call(ctx, "revise_distillation", logged_args, started, result)
    return result


async def add_oob(ctx: TurnContext, args: AddOOBInput) -> AddOOBOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO out_of_bounds (
            owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        args.owner_id,
        args.sensitive_core,
        encrypt_value(args.sensitive_core),
        args.shareable_context,
        args.severity.value,
        args.review_at,
    )
    if args.review_at is not None:
        await _schedule_context_job(
            ctx.pool,
            user_id=args.owner_id,
            job_type="oob_review",
            scheduled_for=args.review_at,
            context_key="oob_id",
            context_id=row["id"],
        )
    result = AddOOBOutput(id=row["id"])
    await _log_tool_call(ctx, "add_oob", args, started, result)
    return result


async def update_oob(ctx: TurnContext, args: UpdateOOBInput) -> UpdateOOBOutput:
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
    row = await ctx.pool.fetchrow(f"UPDATE out_of_bounds SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    if args.review_at is not None:
        owner_id = await ctx.pool.fetchval("SELECT owner_id FROM out_of_bounds WHERE id=$1", args.oob_id)
        await _schedule_context_job(
            ctx.pool,
            user_id=owner_id,
            job_type="oob_review",
            scheduled_for=args.review_at,
            context_key="oob_id",
            context_id=args.oob_id,
        )
    result = UpdateOOBOutput(id=row["id"])
    await _log_tool_call(ctx, "update_oob", args, started, result)
    return result


async def lift_oob(ctx: TurnContext, args: LiftOOBInput) -> LiftOOBOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        "UPDATE out_of_bounds SET status='lifted' WHERE id=$1 RETURNING id, now() AS lifted_at",
        args.oob_id,
    )
    result = LiftOOBOutput(id=row["id"], lifted_at=row["lifted_at"])
    await _log_tool_call(ctx, "lift_oob", args, started, result)
    return result


async def schedule_checkin(ctx: TurnContext, args: ScheduleCheckinInput) -> ScheduleCheckinOutput:
    started = _start()
    scheduled_for = _scheduled_for_from_when_or_delay(args.when, args.delay)
    old, row = await schedule_checkin_record(
        ctx.pool,
        args.user_id,
        scheduled_for=scheduled_for,
        context={"about_what": args.about_what, "reason": args.reason},
    )
    result = ScheduleCheckinOutput(
        job_id=row["job_id"],
        superseded_job_id=old["id"] if old is not None else None,
        scheduled_for=row["scheduled_for"],
    )
    await _log_tool_call(ctx, "schedule_checkin", args, started, result)
    return result


async def cancel_scheduled_checkin(ctx: TurnContext, args: CancelScheduledCheckinInput) -> CancelScheduledCheckinOutput:
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _future_utc(value: datetime, *, field_name: str = "when") -> datetime:
    scheduled_for = _as_utc(value)
    if scheduled_for <= datetime.now(UTC):
        raise ToolCallRejected({"error": "schedule_time_in_past", "field": field_name})
    return scheduled_for


def _delay_delta(delay: ScheduleDelay) -> timedelta:
    return timedelta(weeks=delay.weeks, days=delay.days, hours=delay.hours, minutes=delay.minutes)


def _scheduled_for_from_when_or_delay(when: datetime | None, delay: ScheduleDelay | None) -> datetime:
    if delay is not None:
        return datetime.now(UTC) + _delay_delta(delay)
    if when is None:
        raise ToolCallRejected({"error": "missing_schedule_time", "field": "when"})
    return _future_utc(when)


def _scheduled_task_recurrence_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return normalize_recurrence(raw)


def _scheduled_task_context(*, task_id: Any, brief: str, recurrence: dict[str, Any] | None) -> dict[str, Any]:
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


def _scheduled_task_row(row: Any) -> ScheduledTaskRow:
    context = _row_value(row, "context", {})
    return ScheduledTaskRow(
        task_id=context["task_id"],
        job_id=_row_value(row, "job_id", _row_value(row, "id")),
        brief=context["brief"],
        scheduled_for=row["scheduled_for"],
        recurrence=context.get("recurrence"),
        delayed=bool(_row_value(row, "delayed", False)),
        created_at=_row_value(row, "created_at"),
    )


def _scheduled_task_target(args: UpdateScheduledTaskInput | CancelScheduledTaskInput, ctx: TurnContext) -> tuple[Any | None, str | None]:
    if args.current_task:
        current = current_scheduled_task(ctx)
        if current is None:
            raise ToolCallRejected({"error": "current_task=true is only valid during a scheduled_task turn"})
        return current["job_id"], None
    return args.job_id, str(args.task_id) if args.task_id is not None else None


async def list_scheduled_tasks(ctx: TurnContext, args: ListScheduledTasksInput) -> ListScheduledTasksOutput:
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
    result = ListScheduledTasksOutput(tasks=[_scheduled_task_row(row) for row in rows])
    await _log_tool_call(ctx, "list_scheduled_tasks", args, started, result)
    return result


async def schedule_task(ctx: TurnContext, args: ScheduleTaskInput) -> ScheduleTaskOutput:
    started = _start()
    task_id = uuid4()
    recurrence = _scheduled_task_recurrence_payload(args.recurrence)
    scheduled_for = _scheduled_for_from_when_or_delay(args.when, args.delay)
    context = _scheduled_task_context(task_id=task_id, brief=args.brief, recurrence=recurrence)
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status)
        VALUES ($1, 'scheduled_task', $2, $3::jsonb, 'pending')
        RETURNING id AS job_id, scheduled_for, context
        """,
        ctx.user.id,
        scheduled_for,
        context,
    )
    result = ScheduleTaskOutput(
        task_id=task_id,
        job_id=row["job_id"],
        scheduled_for=row["scheduled_for"],
        recurrence=recurrence,
    )
    await _log_tool_call(ctx, "schedule_task", args, started, result)
    return result


def _reject_unauthorized_current_scheduled_task(ctx: TurnContext, current_task: bool) -> None:
    if current_task and current_scheduled_task(ctx) is None:
        raise ToolCallRejected({"error": "current_task=true is only valid during a scheduled_task turn"})


async def update_scheduled_task(ctx: TurnContext, args: UpdateScheduledTaskInput) -> UpdateScheduledTaskOutput:
    started = _start()
    target_job_id, target_task_id = _scheduled_task_target(args, ctx)
    scheduled_for = (
        _scheduled_for_from_when_or_delay(args.when, args.delay)
        if args.when is not None or args.delay is not None
        else None
    )
    context_patch: dict[str, Any] = {}
    if args.brief is not None:
        context_patch["brief"] = args.brief
    recurrence_was_set = "recurrence" in args.model_fields_set
    if recurrence_was_set:
        context_patch["recurrence"] = _scheduled_task_recurrence_payload(args.recurrence)
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
        result = UpdateScheduledTaskOutput(action="noop", job_id=target_job_id, task_id=target_task_id)
    else:
        task = _scheduled_task_row(row)
        result = UpdateScheduledTaskOutput(
            action="updated",
            task_id=task.task_id,
            job_id=task.job_id,
            scheduled_for=task.scheduled_for,
            recurrence=task.recurrence,
        )
    await _log_tool_call(ctx, "update_scheduled_task", args, started, result)
    return result


async def cancel_scheduled_task(ctx: TurnContext, args: CancelScheduledTaskInput) -> CancelScheduledTaskOutput:
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
        result = CancelScheduledTaskOutput(action="noop", job_id=target_job_id, task_id=target_task_id)
    else:
        context = row["context"]
        result = CancelScheduledTaskOutput(
            action="cancelled",
            job_id=row["job_id"],
            task_id=context.get("task_id"),
        )
    await _log_tool_call(ctx, "cancel_scheduled_task", args, started, result)
    return result


async def escalate_to_partner(ctx: TurnContext, args: EscalateToPartnerInput) -> EscalateToPartnerOutput:
    started = _start()
    if args.from_user_id != ctx.user.id or args.to_user_id != ctx.partner.id:
        logger.warning("escalate_to_partner overriding model-supplied IDs for turn_id=%s", ctx.turn_id)
    allowed_by_crisis = ctx.trigger_charge == "crisis"
    allowed_by_explicit_request = ctx.explicit_partner_alert_requested
    if not allowed_by_crisis and not allowed_by_explicit_request:
        result = {
            "error": "escalation_rejected",
            "reason": "escalate_to_partner requires trusted crisis charge or explicit partner-alert request before sending",
        }
        await _log_tool_call(ctx, "escalate_to_partner", args, started, result)
        raise ToolCallRejected(result)
    template = TemplateCall(name="escalation", params=[ctx.partner.name, ctx.user.name, args.content])
    out_id = await send_outbound(
        ctx.pool,
        ctx.partner,
        args.content,
        template_fallback=template,
        bot_turn_id=ctx.turn_id,
        protected_owner_ids=[ctx.user.id, ctx.partner.id],
    )
    await _append_turn_reasoning(
        ctx.pool,
        ctx.turn_id,
        f"ESCALATION_SENT gate={'crisis' if allowed_by_crisis else 'explicit_partner_alert'} reason={args.reason} outbound_message_id={out_id}",
    )
    result = EscalateToPartnerOutput(action="sent", outbound_message_id=out_id, used_template=False, reason_if_deferred=None)
    await _log_tool_call(ctx, "escalate_to_partner", args, started, result)
    return result


async def _fetch_dyad_message(ctx: TurnContext, message_id: Any) -> Any | None:
    return await ctx.pool.fetchrow(
        """
        SELECT id, direction, sender_id, recipient_id, content, whatsapp_message_id, deleted_at
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


async def edit_outbound_message(ctx: TurnContext, args: EditOutboundMessageInput) -> EditOutboundMessageOutput:
    started = _start()
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

    verdict = await _call_oob_hook(ctx.pool, args.content, row["recipient_id"], [ctx.user.id, ctx.partner.id])
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

    recipient_phone = ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    await discord.edit_text(recipient_phone, row["whatsapp_message_id"], args.content)
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
    result = EditOutboundMessageOutput(
        action="edited",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        reason=args.reason,
    )
    await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
    return result


async def delete_outbound_message(ctx: TurnContext, args: DeleteOutboundMessageInput) -> DeleteOutboundMessageOutput:
    started = _start()
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

    recipient_phone = ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    await discord.delete_text(recipient_phone, row["whatsapp_message_id"])
    await ctx.pool.execute(
        "UPDATE messages SET deleted_at = now(), processing_state='expired' WHERE id=$1",
        args.message_id,
    )
    result = DeleteOutboundMessageOutput(
        action="deleted",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        reason=args.reason,
    )
    await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
    return result


async def react_to_message(ctx: TurnContext, args: ReactToMessageInput) -> ReactToMessageOutput:
    started = _start()
    row = await _fetch_dyad_message(ctx, args.message_id)
    if row is None or row["whatsapp_message_id"] is None or row["deleted_at"] is not None:
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
        target_phone = ctx.user.phone if row["sender_id"] == ctx.user.id else ctx.partner.phone
    else:
        target_phone = ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    await discord.add_reaction(target_phone, row["whatsapp_message_id"], args.emoji)
    result = ReactToMessageOutput(
        action="reacted",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        emoji=args.emoji,
        reason=args.reason,
    )
    await _log_tool_call(ctx, "react_to_message", args, started, result)
    return result


async def explain_media_item(ctx: TurnContext, args: ExplainMediaItemInput) -> ExplainMediaItemOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        SELECT id, direction, sender_id, recipient_id, media_type, media_url, deleted_at
        FROM messages
        WHERE id=$1
          AND (
            sender_id = ANY($2::uuid[])
            OR recipient_id = ANY($2::uuid[])
          )
        """,
        args.message_id,
        [ctx.user.id, ctx.partner.id],
    )
    if row is None or row["deleted_at"] is not None:
        result = ExplainMediaItemOutput(action="not_found", message_id=args.message_id, reason="message not found")
        await _log_tool_call(ctx, "explain_media_item", args, started, result)
        return result
    owner_id = _message_thread_owner_id(row)
    sharing_default = ctx.user.cross_thread_sharing_default if owner_id == ctx.user.id else ctx.partner.cross_thread_sharing_default
    if not raw_message_visibility(
        viewer_user_id=ctx.user.id,
        thread_owner_user_id=owner_id,
        thread_owner_sharing_default=sharing_default,
    ).visible:
        result = ExplainMediaItemOutput(
            action="blocked",
            message_id=args.message_id,
            media_type=row["media_type"],
            reason="raw partner media hidden by sharing_default",
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
    await _log_tool_call(ctx, "explain_media_item", args, started, result)
    return result


async def log_feedback(ctx: TurnContext, args: LogFeedbackInput) -> LogFeedbackOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO feedback (from_user_id, target_type, target_id, sentiment, content, source)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        args.from_user_id,
        args.target_type,
        args.target_id,
        args.sentiment.value,
        args.content,
        args.source,
    )
    result = LogFeedbackOutput(id=row["id"])
    await _log_tool_call(ctx, "log_feedback", args, started, result)
    return result
