"""Read-only tools for the agentic loop."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.services.turn_context import TurnContext
from app.services.oob_check import check_oob_with_policy, summarize_partner_oob
from app.services.tools.common import (
    add_date_range,
    memory_row,
    message_hit,
    observation_row,
    oob_row,
    theme_summary,
    value,
    watch_item_row,
)
from tool_schemas import (
    BotAction,
    CheckOOBInput,
    CheckOOBOutput,
    DateRange,
    GetBotActionsInput,
    GetBotActionsOutput,
    GetMemoriesInput,
    GetMemoriesOutput,
    GetOOBInput,
    GetOOBOutput,
    GetObservationsInput,
    GetObservationsOutput,
    GetSelfModelInput,
    GetSelfModelOutput,
    GetThemeInput,
    GetThemeOutput,
    ListThemesInput,
    ListThemesOutput,
    ListWatchItemsInput,
    ListWatchItemsOutput,
    OOBVerdict,
    RecentActivityInput,
    RecentActivityOutput,
    SearchMessagesInput,
    SearchMessagesOutput,
    SelfModel,
    SummarizeOOBTopicsInput,
    SummarizeOOBTopicsOutput,
    ThemeDetail,
    ThreadDigest,
)

logger = logging.getLogger(__name__)


async def search_messages(ctx: TurnContext, args: SearchMessagesInput) -> SearchMessagesOutput:
    logger.info("read tool search_messages turn_id=%s", ctx.turn_id)
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if args.partner_user_id is not None:
        params.append(args.partner_user_id)
        clauses.append(f"(sender_id = ${len(params)} OR recipient_id = ${len(params)})")
    if args.text_contains:
        params.append(f"%{args.text_contains}%")
        clauses.append(f"content ILIKE ${len(params)}")
    add_date_range(clauses, params, "sent_at", args.date_range)
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, sender_id, sent_at, content, COALESCE(charge, 'routine') AS charge, direction
        FROM messages
        WHERE {' AND '.join(clauses)}
        ORDER BY sent_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return SearchMessagesOutput(hits=[message_hit(row) for row in rows], truncated=len(rows) == args.limit)


async def recent_activity(ctx: TurnContext, args: RecentActivityInput) -> RecentActivityOutput:
    logger.info("read tool recent_activity turn_id=%s", ctx.turn_id)
    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    rows = await ctx.pool.fetch(
        """
        SELECT u.id AS user_id, u.name AS user_name, COUNT(m.id) AS message_count,
               MAX(m.sent_at) AS last_message_at,
               (ARRAY_AGG(m.content ORDER BY m.sent_at DESC))[1] AS latest_content
        FROM users u
        LEFT JOIN messages m
          ON (m.sender_id = u.id OR m.recipient_id = u.id)
         AND m.sent_at >= $1
         AND m.sent_at <= $2
         AND m.deleted_at IS NULL
        GROUP BY u.id, u.name
        ORDER BY last_message_at DESC NULLS LAST, u.name ASC
        """,
        start,
        end,
    )
    threads: list[ThreadDigest] = []
    for row in rows:
        count = int(value(row, "message_count", 0))
        snippet = (value(row, "latest_content", "") or "")[:160]
        # Plan 3 stub. tool_schemas.ThreadDigest.summary describes an LLM-generated digest; deferring the Haiku digest to Plan 4 alongside the significance scorer.
        summary = f'{count} messages this period; latest: "{snippet}"'
        threads.append(
            ThreadDigest(
                user_id=row["user_id"],
                user_name=row["user_name"],
                message_count=count,
                last_message_at=row["last_message_at"],
                summary=summary,
            )
        )
    return RecentActivityOutput(threads=threads, period=DateRange(start=start, end=end))


async def list_themes(ctx: TurnContext, args: ListThemesInput) -> ListThemesOutput:
    logger.info("read tool list_themes turn_id=%s", ctx.turn_id)
    order_by = {
        "last_reinforced": "COALESCE(last_reinforced_at, first_seen_at) DESC",
        "last_active": "last_active_at DESC",
        "created": "first_seen_at DESC",
    }[args.sort_by.value]
    status_clause = "WHERE status = 'active'" if args.active_only else ""
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, title, status, sentiment, health, last_reinforced_at, last_active_at
        FROM themes
        {status_clause}
        ORDER BY {order_by}, title ASC
        LIMIT $1
        """,
        args.limit,
    )
    return ListThemesOutput(themes=[theme_summary(row) for row in rows])


async def get_theme(ctx: TurnContext, args: GetThemeInput) -> GetThemeOutput:
    logger.info("read tool get_theme turn_id=%s", ctx.turn_id)
    row = await ctx.pool.fetchrow(
        """
        SELECT id, title, description, status, sentiment, health, first_seen_at,
               last_reinforced_at, last_active_at
        FROM themes
        WHERE id = $1
        """,
        args.theme_id,
    )
    if row is None:
        return GetThemeOutput(theme=None)
    memory_rows = await ctx.pool.fetch(
        "SELECT id FROM memories WHERE $1 = ANY(COALESCE(related_theme_ids, '{}'::uuid[]))",
        args.theme_id,
    )
    observation_rows = await ctx.pool.fetch(
        "SELECT id FROM observations WHERE $1 = ANY(COALESCE(related_theme_ids, '{}'::uuid[]))",
        args.theme_id,
    )
    return GetThemeOutput(
        theme=ThemeDetail(
            **theme_summary(row).model_dump(),
            description=row["description"],
            first_seen_at=row["first_seen_at"],
            related_memory_ids=[r["id"] for r in memory_rows],
            related_observation_ids=[r["id"] for r in observation_rows],
        )
    )


async def get_memories(ctx: TurnContext, args: GetMemoriesInput) -> GetMemoriesOutput:
    logger.info("read tool get_memories turn_id=%s", ctx.turn_id)
    clauses = ["status = $1"]
    params: list[Any] = [args.status.value]
    if args.couple_only:
        clauses.append("about_user_id IS NULL")
    elif args.about_user_id is not None:
        params.append(args.about_user_id)
        clauses.append(f"about_user_id = ${len(params)}")
    if args.theme_id is not None:
        params.append(args.theme_id)
        clauses.append(f"${len(params)} = ANY(COALESCE(related_theme_ids, '{{}}'::uuid[]))")
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, about_user_id, content, status, COALESCE(related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               created_at, last_referenced_at
        FROM memories
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(last_referenced_at, created_at) DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return GetMemoriesOutput(memories=[memory_row(row) for row in rows])


async def list_watch_items(ctx: TurnContext, args: ListWatchItemsInput) -> ListWatchItemsOutput:
    logger.info("read tool list_watch_items turn_id=%s", ctx.turn_id)
    clauses: list[str] = []
    params: list[Any] = []
    if args.owner_user_id is not None:
        params.append(args.owner_user_id)
        clauses.append(f"owner_user_id = ${len(params)}")
    if args.status is not None:
        params.append(args.status.value)
        clauses.append(f"status = ${len(params)}")
    if args.due_before is not None:
        params.append(args.due_before)
        clauses.append(f"due_at <= ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, owner_user_id, content, due_at, status, addressing_note, created_at, addressed_at,
               COALESCE(related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids
        FROM watch_items
        {where}
        ORDER BY COALESCE(due_at, created_at) ASC
        """,
        *params,
    )
    return ListWatchItemsOutput(items=[watch_item_row(row) for row in rows])


async def get_observations(ctx: TurnContext, args: GetObservationsInput) -> GetObservationsOutput:
    logger.info("read tool get_observations turn_id=%s", ctx.turn_id)
    clauses = ["status = $1"]
    params: list[Any] = [args.status.value]
    if args.theme_id is not None:
        params.append(args.theme_id)
        clauses.append(f"${len(params)} = ANY(COALESCE(related_theme_ids, '{{}}'::uuid[]))")
    if args.about_user_id is not None:
        params.append(args.about_user_id)
        clauses.append(f"about_user_id = ${len(params)}")
    if args.min_significance is not None:
        params.append(args.min_significance)
        clauses.append(f"significance >= ${len(params)}")
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, content, about_user_id, confidence, significance, status,
               COALESCE(related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               COALESCE(supporting_message_ids, '{{}}'::uuid[]) AS supporting_message_ids,
               created_at, last_reinforced_at, surfaced_count
        FROM observations
        WHERE {' AND '.join(clauses)}
        ORDER BY recency_weighted_score(significance, last_reinforced_at, created_at) DESC NULLS LAST,
                 COALESCE(last_reinforced_at, created_at) DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return GetObservationsOutput(observations=[observation_row(row) for row in rows])


async def get_oob(ctx: TurnContext, args: GetOOBInput) -> GetOOBOutput:
    logger.info("read tool get_oob turn_id=%s", ctx.turn_id)
    clauses: list[str] = []
    params: list[Any] = []
    if args.owner_id is not None:
        params.append(args.owner_id)
        clauses.append(f"owner_id = ${len(params)}")
    if not args.include_lifted:
        clauses.append("status = 'active'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, owner_id, shareable_context, severity, status, created_at, review_at
        FROM out_of_bounds
        {where}
        ORDER BY created_at DESC
        """,
        *params,
    )
    return GetOOBOutput(entries=[oob_row(row) for row in rows])


async def check_oob(ctx: TurnContext, args: CheckOOBInput) -> CheckOOBOutput:
    logger.info("read tool check_oob turn_id=%s", ctx.turn_id)
    return await check_oob_with_policy(
        ctx.pool,
        content=args.content,
        recipient_id=args.recipient_id,
        protected_owner_ids=args.protected_owner_ids,
        sender_intent=args.sender_intent,
    )


async def summarize_oob_topics(ctx: TurnContext, args: SummarizeOOBTopicsInput) -> SummarizeOOBTopicsOutput:
    logger.info("read tool summarize_oob_topics turn_id=%s", ctx.turn_id)
    return await summarize_partner_oob(ctx.pool, owner_id=args.owner_id)


async def get_self_model(ctx: TurnContext, args: GetSelfModelInput) -> GetSelfModelOutput:
    logger.info("read tool get_self_model turn_id=%s", ctx.turn_id)
    user_row = await ctx.pool.fetchrow(
        "SELECT id, name, COALESCE(style_notes, '') AS style_notes FROM users WHERE id = $1",
        args.user_id,
    )
    if user_row is None:
        raise ValueError(f"user not found: {args.user_id}")
    themes = await list_themes(ctx, ListThemesInput(active_only=True, limit=10))
    memories = await get_memories(ctx, GetMemoriesInput(about_user_id=args.user_id))
    observations = await get_observations(
        ctx,
        GetObservationsInput(about_user_id=args.user_id, min_significance=3),
    )
    watch_items = await list_watch_items(ctx, ListWatchItemsInput(owner_user_id=args.user_id))
    return GetSelfModelOutput(
        model=SelfModel(
            user_id=user_row["id"],
            name=user_row["name"],
            style_notes=user_row["style_notes"],
            active_themes=themes.themes,
            memories=memories.memories,
            high_significance_observations=observations.observations,
            open_watch_items=watch_items.items,
        )
    )


def _target_tool_names(target_type: Any) -> set[str]:
    value = target_type.value if target_type is not None else None
    return {
        "message": {"escalate_to_partner"},
        "memory": {"add_memory", "update_memory", "supersede_memory"},
        "observation": {"log_observation", "update_observation"},
        "theme": {"create_theme", "update_theme"},
        "watch_item": {"add_watch_item", "update_watch_item", "address_watch_item"},
        "oob": {"add_oob", "update_oob", "lift_oob"},
        "schedule": {"schedule_checkin", "cancel_scheduled_checkin"},
        "escalation": {"escalate_to_partner"},
    }.get(value, set())


async def get_bot_actions(ctx: TurnContext, args: GetBotActionsInput) -> GetBotActionsOutput:
    logger.info("read tool get_bot_actions turn_id=%s", ctx.turn_id)
    clauses: list[str] = []
    params: list[Any] = []
    add_date_range(clauses, params, "bt.started_at", args.date_range)
    if args.user_in_context is not None:
        params.append(args.user_in_context)
        clauses.append(f"bt.user_in_context = ${len(params)}")
    target_names = _target_tool_names(args.target_type)
    if target_names:
        params.append(list(target_names))
        clauses.append(
            f"EXISTS (SELECT 1 FROM tool_calls tcf WHERE tcf.turn_id = bt.id AND tcf.tool_name = ANY(${len(params)}::text[]))"
        )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT bt.id AS turn_id, bt.started_at, bt.user_in_context, bt.triggered_by_message_id,
               tm.content AS triggering_content,
               bt.final_output_message_id, om.content AS final_outbound_content,
               COALESCE(bt.reasoning, '') AS reasoning,
               COALESCE(
                 jsonb_agg(to_jsonb(tc) ORDER BY tc.called_at) FILTER (WHERE tc.id IS NOT NULL),
                 '[]'::jsonb
               ) AS tool_calls
        FROM bot_turns bt
        LEFT JOIN messages tm ON tm.id = bt.triggered_by_message_id
        LEFT JOIN messages om ON om.id = bt.final_output_message_id
        LEFT JOIN tool_calls tc ON tc.turn_id = bt.id
        {where}
        GROUP BY bt.id, tm.content, om.content
        ORDER BY bt.started_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return GetBotActionsOutput(
        actions=[
            BotAction(
                turn_id=row["turn_id"],
                started_at=row["started_at"],
                user_in_context=row["user_in_context"],
                triggered_by_message_id=row["triggered_by_message_id"],
                final_output_message_id=row["final_output_message_id"],
                triggering_content=row["triggering_content"],
                final_outbound_content=row["final_outbound_content"],
                reasoning=row["reasoning"],
                tool_calls=list(row["tool_calls"] or []),
            )
            for row in rows
        ]
    )
