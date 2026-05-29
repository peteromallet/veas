"""Read-only tools for the agentic loop."""

from __future__ import annotations

import logging
import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.services.cross_thread_privacy import raw_message_visibility
from app.services.cross_thread_privacy import bridge_candidate_visible_to_target
from app.services.partner_sharing import get_partner_share
from app.services.turn_context import TurnContext, scope_from_turn_context
from app.config import get_settings
from app.services.messaging import send_outbound_part
from app.services.oob_check import check_oob_with_policy, summarize_partner_oob
from app.services.text_safety import (
    clean_user_facing_text,
    looks_like_internal_process_text,
)
from app.services.time_context import local_day_bounds_utc, temporal_reference
from app.services.scheduled_task_recurrence import normalize_recurrence
from app.services.tools.scope_guard import check_read_scope
from app.services.tools.common import (
    add_date_range,
    distillation_row,
    memory_row,
    message_hit,
    observation_row,
    oob_row,
    theme_summary,
    value,
    watch_item_row,
)
from app.services.topic_filter import join_artifact_topics
from app.services.live.plan_markdown import agenda_to_display
from tool_schemas import (
    BotAction,
    BridgeCandidate,
    CheckOOBInput,
    CheckOOBOutput,
    DateRange,
    GetDistillationsInput,
    GetDistillationsOutput,
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
    GetToolCallInput,
    GetToolCallOutput,
    ToolCallDetail,
    GetThemeInput,
    GetThemeOutput,
    ListBridgeCandidatesInput,
    ListBridgeCandidatesOutput,
    ListScheduledCheckinsInput,
    ListScheduledCheckinsOutput,
    ListThemesInput,
    ListThemesOutput,
    ListWatchItemsInput,
    ListWatchItemsOutput,
    RecentActivityInput,
    RecentActivityOutput,
    EmojiSearchHit,
    SearchMessagesInput,
    SearchMessagesOutput,
    SearchEmojisInput,
    SearchEmojisOutput,
    ListAllRemindersInput,
    ListAllRemindersOutput,
    ReminderItem,
    ScheduledCheckinRow,
    SelfModel,
    SendMessagePartInput,
    SendMessagePartOutput,
    SummarizeOOBTopicsInput,
    SummarizeOOBTopicsOutput,
    ThemeDetail,
    ThreadDigest,
    # hector
    ListCommitmentsInput,
    ListCommitmentsOutput,
    CommitmentSummary,
    ListEventsInput,
    ListEventsOutput,
    EventSummary,
    GetAdherenceInput,
    GetAdherenceOutput,
    CommitmentAdherence,
    AdherenceSlot,
    # plan tools
    ReadConversationPlanInput,
    ReadConversationPlanOutput,
    ListConversationPlansInput,
    ListConversationPlansOutput,
    ListConversationPlansRow,
    PlanItem,
)

logger = logging.getLogger(__name__)

_EMOJI_FALLBACK = {
    "🫶": ("heart hands", ["care", "support", "tender", "warmth"]),
    "🕯️": ("candle", ["gentle", "grief", "quiet", "holding space"]),
    "🪷": ("lotus", ["calm", "patience", "growth", "softness"]),
    "🧭": ("compass", ["direction", "orientation", "finding way"]),
    "🪨": ("rock", ["steady", "grounded", "solid", "weight"]),
    "🌿": ("herb", ["gentle", "repair", "fresh", "peace"]),
    "🧵": ("thread", ["connection", "follow", "story", "continuity"]),
    "🪞": ("mirror", ["reflection", "seeing", "self-awareness"]),
    "🌫️": ("fog", ["unclear", "confusing", "blurred"]),
    "🛟": ("ring buoy", ["help", "support", "rescue"]),
    "🧩": ("puzzle piece", ["missing piece", "complex", "fit"]),
    "🤲": ("palms up together", ["offering", "gentle", "receiving"]),
}


async def _partner_share_by_user_for_current_bot(ctx: TurnContext) -> dict[Any, str]:
    users_by_id = {ctx.user.id: ctx.user}
    if ctx.partner is not None:
        users_by_id[ctx.partner.id] = ctx.partner
    if ctx.bot_id is None:
        return {user_id: "unset" for user_id in users_by_id}
    states: dict[Any, str] = {}
    for user_id, user in users_by_id.items():
        partner_share = await get_partner_share(
            ctx.pool, user_id=user_id, bot_id=ctx.bot_id
        )
        if partner_share is None:
            legacy_key = "cross_thread_" + "sharing" + "_default"
            partner_share = getattr(user, legacy_key, None)
        states[user_id] = partner_share or "unset"
    return states


def _message_in_current_scope(row: Any, ctx: TurnContext) -> bool:
    return (
        ctx.bot_id is not None
        and ctx.primary_topic_id is not None
        and value(row, "bot_id") == ctx.bot_id
        and value(row, "topic_id") == ctx.primary_topic_id
    )


async def _partner_share_for_owner_bot(
    ctx: TurnContext, cache: dict[tuple[Any, str], str], owner_id: Any, bot_id: Any
) -> str:
    if owner_id == ctx.user.id:
        return "opt_in"
    if bot_id is None:
        return "unset"
    cache_key = (owner_id, str(bot_id))
    if cache_key not in cache:
        cache[cache_key] = (
            await get_partner_share(ctx.pool, user_id=owner_id, bot_id=str(bot_id))
            or "unset"
        )
    return cache[cache_key]


def _ctx_timezone(ctx: TurnContext, override: str | None = None) -> str:
    return override or ctx.user.timezone or "UTC"


def _ctx_now(ctx: TurnContext) -> datetime:
    return ctx.turn_started_at or datetime.now(UTC)


def _coerce_datetime(value_: Any) -> datetime | None:
    if value_ is None:
        return None
    if isinstance(value_, datetime):
        return value_
    if isinstance(value_, str):
        text = value_.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _time(
    value_: datetime | str | None, ctx: TurnContext, *, timezone: str | None = None
) -> dict[str, str] | None:
    value_dt = _coerce_datetime(value_)
    if value_dt is None:
        return None
    return temporal_reference(value_dt, _ctx_timezone(ctx, timezone), now=_ctx_now(ctx))


def _with_audit_event_times(events: list[dict], ctx: TurnContext) -> list[dict]:
    out = []
    for event in events:
        item = dict(event)
        if item.get("occurred_at") is not None:
            item["occurred_at_time"] = _time(item["occurred_at"], ctx)
        out.append(item)
    return out


_EMOJI_QUERY_EXPANSIONS = {
    "support": {"help", "hand", "hands", "holding", "hug", "care", "heart", "buoy"},
    "quiet": {"silence", "muted", "hushed", "candle", "fog", "night", "peace"},
    "fragile": {"crack", "cracked", "egg", "glass", "feather", "wilted"},
    "repair": {"mending", "thread", "needle", "tool", "wrench", "seedling", "bridge"},
    "stuck": {"knot", "puzzle", "maze", "lock", "anchor"},
    "steady": {"rock", "anchor", "mountain", "compass"},
    "soft": {"feather", "cloud", "lotus", "herb", "palms"},
    "sad": {"rain", "cloud", "wilted", "candle", "blue"},
    "progress": {"seedling", "sprout", "step", "sunrise", "chart"},
    "bridge": {"bridge", "thread", "link", "handshake", "compass"},
    "confusing": {"fog", "maze", "question", "mirror", "puzzle"},
}


def _emoji_terms(value: str) -> set[str]:
    terms = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}
    expanded = set(terms)
    for term in terms:
        expanded.update(_EMOJI_QUERY_EXPANSIONS.get(term, set()))
    return expanded


def _emoji_score(
    query_terms: set[str], name: str, aliases: list[str], keywords: list[str]
) -> int:
    haystacks = [name, *aliases, *keywords]
    score = 0
    for term in query_terms:
        for idx, haystack in enumerate(haystacks):
            normalized = haystack.lower().replace("_", " ").replace("-", " ")
            if term == normalized:
                score += 12 if idx == 0 else 8
            elif term in _emoji_terms(normalized):
                score += 6 if idx == 0 else 4
            elif term in normalized:
                score += 2
    return score


class NewerInboundDuringPacedSend(Exception):
    pass


async def _newer_inbound_exists(ctx: TurnContext) -> bool:
    boundary = ctx.turn_started_at
    if ctx.triggering_message_ids:
        trigger_boundary = await ctx.pool.fetchval(
            "SELECT MAX(sent_at) FROM messages WHERE id = ANY($1::uuid[])",
            ctx.triggering_message_ids,
        )
        if trigger_boundary is not None:
            boundary = trigger_boundary
    if boundary is None:
        return False
    return bool(
        await ctx.pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM messages
                WHERE direction='inbound'
                  AND sender_id=$1
                  AND sent_at > $2
                  AND NOT (id = ANY($3::uuid[]))
                  AND bot_id = $4
            )
            """,
            ctx.user.id,
            boundary,
            ctx.triggering_message_ids,
            ctx.bot_id,
        )
    )


async def send_message_part(
    ctx: TurnContext, args: SendMessagePartInput
) -> SendMessagePartOutput:
    logger.info("read tool send_message_part turn_id=%s", ctx.turn_id)
    settings = get_settings()
    sent_parts = ctx.sent_message_parts
    if sent_parts is None:
        sent_parts = []
        ctx.sent_message_parts = sent_parts
    if (
        not ctx.incremental_sending_enabled
        or settings.messaging_provider.strip().lower() != "discord"
        or not settings.discord_multi_message_enabled
    ):
        return SendMessagePartOutput(
            status="not_enabled",
            client_part_key=args.client_part_key,
            visible_to_user=False,
            sent_so_far=[part["content"] for part in sent_parts],
            reason="incremental message parts are not enabled for this turn",
        )
    if len(sent_parts) >= settings.discord_multi_message_max_parts:
        return SendMessagePartOutput(
            status="withheld",
            client_part_key=args.client_part_key,
            visible_to_user=False,
            sent_so_far=[part["content"] for part in sent_parts],
            reason="maximum message parts reached for this turn",
        )
    if await _newer_inbound_exists(ctx):
        return SendMessagePartOutput(
            status="interrupted",
            client_part_key=args.client_part_key,
            visible_to_user=False,
            sent_so_far=[part["content"] for part in sent_parts],
            reason="a newer inbound message arrived while this turn was running",
        )

    content = args.content.strip()
    if (
        looks_like_internal_process_text(content)
        or clean_user_facing_text(content).strip() == ""
    ):
        return SendMessagePartOutput(
            status="withheld",
            client_part_key=args.client_part_key,
            visible_to_user=False,
            sent_so_far=[part["content"] for part in sent_parts],
            reason="content looks like internal process narration (memory IDs, write plans, phase notes); send a user-facing reply instead",
        )
    part_index = len(sent_parts) + 1
    part_key = f"{ctx.turn_id}:{part_index}"
    paced_send_available = (
        ctx.before_paced_send is not None and not ctx.send_typing_indicator
    )
    if (
        sent_parts
        and settings.discord_multi_message_delay_s > 0
        and not paced_send_available
    ):
        await asyncio.sleep(settings.discord_multi_message_delay_s)
        if await _newer_inbound_exists(ctx):
            return SendMessagePartOutput(
                status="interrupted",
                client_part_key=args.client_part_key,
                visible_to_user=False,
                sent_so_far=[part["content"] for part in sent_parts],
                reason="a newer inbound message arrived before the next message part",
            )
    before_provider_send = None
    if paced_send_available:
        send_kind = "incremental_first" if part_index == 1 else "incremental_next"

        async def before_provider_send(
            text: str = content, kind: str = send_kind, index: int = part_index
        ) -> None:
            await ctx.before_paced_send(text, send_kind=kind, part_index=index)
            if await _newer_inbound_exists(ctx):
                raise NewerInboundDuringPacedSend()

    try:
        result = await send_outbound_part(
            ctx.pool,
            ctx.user,
            content,
            bot_turn_id=ctx.turn_id,
            part_key=part_key,
            part_index=part_index,
            client_part_key=args.client_part_key,
            protected_owner_ids=ctx.protected_owner_ids,
            send_typing_indicator=ctx.send_typing_indicator,
            before_provider_send=before_provider_send,
            scope=scope_from_turn_context(ctx),
        )
    except NewerInboundDuringPacedSend:
        return SendMessagePartOutput(
            status="interrupted",
            client_part_key=args.client_part_key,
            visible_to_user=False,
            sent_so_far=[part["content"] for part in sent_parts],
            reason="a newer inbound message arrived before the next message part",
        )
    output = SendMessagePartOutput.model_validate(result)
    if (
        output.visible_to_user
        and output.message_id is not None
        and output.delivered_content
    ):
        sent_parts.append(
            {
                "message_id": output.message_id,
                "provider_message_id": output.provider_message_id,
                "content": output.delivered_content,
                "part_key": output.part_key,
            }
        )
        await ctx.pool.execute(
            "UPDATE bot_turns SET final_output_message_id=$1 WHERE id=$2",
            output.message_id,
            ctx.turn_id,
        )
    return output


async def search_messages(
    ctx: TurnContext, args: SearchMessagesInput
) -> SearchMessagesOutput:
    logger.info("read tool search_messages turn_id=%s", ctx.turn_id)
    if ctx.bot_id is None or ctx.primary_topic_id is None:
        return SearchMessagesOutput(hits=[], truncated=False)
    participant_ids = [ctx.user.id]
    if ctx.partner is not None:
        participant_ids.append(ctx.partner.id)
    dyad_ids = set(participant_ids)
    if args.partner_user_id is not None and args.partner_user_id not in dyad_ids:
        return SearchMessagesOutput(hits=[], truncated=False)
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if args.partner_user_id is not None:
        params.append(args.partner_user_id)
        clauses.append(f"(sender_id = ${len(params)} OR recipient_id = ${len(params)})")
    else:
        params.append(participant_ids)
        clauses.append(
            f"(sender_id = ANY(${len(params)}::uuid[]) OR recipient_id = ANY(${len(params)}::uuid[]))"
        )
    params.append(ctx.bot_id)
    clauses.append(f"bot_id = ${len(params)}")
    params.append(ctx.primary_topic_id)
    clauses.append(f"topic_id = ${len(params)}")
    if args.text_contains:
        params.append(f"%{args.text_contains}%")
        clauses.append(
            f"""(
                content ILIKE ${len(params)}
                OR media_analysis->>'explanation' ILIKE ${len(params)}
                OR media_analysis->>'description' ILIKE ${len(params)}
                OR media_analysis->>'summary' ILIKE ${len(params)}
            )"""
        )
    if args.local_day is not None:
        start, end = local_day_bounds_utc(
            args.local_day, _ctx_timezone(ctx, args.timezone), now=_ctx_now(ctx)
        )
        params.extend([start, end])
        clauses.append(f"sent_at >= ${len(params) - 1}")
        clauses.append(f"sent_at < ${len(params)}")
    else:
        add_date_range(clauses, params, "sent_at", args.date_range)
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, sender_id, recipient_id, sent_at, content, media_type, media_analysis, bot_id, topic_id,
               COALESCE(charge, 'routine') AS charge, direction
        FROM messages
        WHERE {' AND '.join(clauses)}
        ORDER BY sent_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    partner_share_by_user = await _partner_share_by_user_for_current_bot(ctx)
    hits = []
    for row in rows:
        if not _message_in_current_scope(row, ctx):
            continue
        owner_id = _message_thread_owner_id(row)
        if owner_id not in dyad_ids:
            continue
        if not raw_message_visibility(
            viewer_user_id=ctx.user.id,
            thread_owner_user_id=owner_id,
            thread_owner_partner_share=partner_share_by_user.get(owner_id),
        ).visible:
            continue
        hits.append(message_hit(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)))
    return SearchMessagesOutput(hits=hits, truncated=len(rows) == args.limit)


async def list_bridge_candidates(
    ctx: TurnContext, args: ListBridgeCandidatesInput
) -> ListBridgeCandidatesOutput:
    logger.info("read tool list_bridge_candidates turn_id=%s", ctx.turn_id)
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return ListBridgeCandidatesOutput(
            is_error=True, error=_err, candidates=[], truncated=False
        )
    if ctx.partner is None:
        return ListBridgeCandidatesOutput(candidates=[], truncated=False)
    rows = await ctx.pool.fetch(
        """
        SELECT id, source_user_id, target_user_id, kind, status, sensitivity, partner_path,
               COALESCE(source_message_ids, '{}'::uuid[]) AS source_message_ids,
               COALESCE(related_memory_ids, '{}'::uuid[]) AS related_memory_ids,
               COALESCE(related_observation_ids, '{}'::uuid[]) AS related_observation_ids,
               internal_note, shareable_summary, sent_message_id,
               created_at, updated_at, resolved_at
        FROM bridge_candidates
        WHERE (
            (source_user_id=$1 AND target_user_id=$2)
            OR (source_user_id=$2 AND target_user_id=$1)
        )
          AND ($3::uuid IS NULL OR source_user_id=$3)
          AND ($4::uuid IS NULL OR target_user_id=$4)
          AND ($5::text IS NULL OR status=$5)
          AND ($6::text IS NULL OR partner_path=$6)
        ORDER BY created_at DESC
        LIMIT $7
        """,
        ctx.user.id,
        ctx.partner.id,
        args.source_user_id,
        args.target_user_id,
        args.status.value if args.status is not None else None,
        args.partner_path.value if args.partner_path is not None else None,
        args.limit,
    )
    candidates: list[BridgeCandidate] = []
    for row in rows:
        if (
            row["target_user_id"] == ctx.user.id
            and row["source_user_id"] != ctx.user.id
        ):
            if not bridge_candidate_visible_to_target(row, target_user_id=ctx.user.id):
                continue
            row = {**dict(row), "internal_note": None}
        candidates.append(_bridge_candidate(row))
    return ListBridgeCandidatesOutput(
        candidates=candidates, truncated=len(rows) == args.limit
    )


def _bridge_candidate(row: Any) -> BridgeCandidate:
    data = dict(row)
    data.setdefault("partner_path", "message_partner")
    data["source_message_ids"] = list(data.get("source_message_ids") or [])
    data["related_memory_ids"] = list(data.get("related_memory_ids") or [])
    data["related_observation_ids"] = list(data.get("related_observation_ids") or [])
    return BridgeCandidate.model_validate(data)


async def search_emojis(
    ctx: TurnContext, args: SearchEmojisInput
) -> SearchEmojisOutput:
    logger.info("read tool search_emojis turn_id=%s", ctx.turn_id)
    query_terms = _emoji_terms(args.query)
    candidates: list[EmojiSearchHit] = []
    used_full_dataset = False

    try:
        import emoji as emoji_pkg  # type: ignore

        used_full_dataset = True
        for symbol, data in emoji_pkg.EMOJI_DATA.items():
            raw_name = str(data.get("en") or "").strip(":").replace("_", " ")
            aliases = [
                str(item).strip(":").replace("_", " ")
                for item in data.get("alias", []) or []
            ]
            keywords = [
                str(item).replace("_", " ") for item in data.get("variant", []) or []
            ]
            if symbol in _EMOJI_FALLBACK:
                fallback_name, fallback_keywords = _EMOJI_FALLBACK[symbol]
                aliases.append(fallback_name)
                keywords.extend(fallback_keywords)
            score = _emoji_score(query_terms, raw_name, aliases, keywords)
            if score > 0:
                candidates.append(
                    EmojiSearchHit(
                        emoji=symbol,
                        name=raw_name,
                        aliases=aliases,
                        keywords=keywords,
                        score=score,
                    )
                )
    except Exception:
        for symbol, (name, keywords) in _EMOJI_FALLBACK.items():
            score = _emoji_score(query_terms, name, [], keywords)
            if score > 0:
                candidates.append(
                    EmojiSearchHit(
                        emoji=symbol,
                        name=name,
                        keywords=keywords,
                        score=score,
                    )
                )

    candidates.sort(key=lambda hit: (-hit.score, len(hit.name), hit.name, hit.emoji))
    return SearchEmojisOutput(
        query=args.query,
        hits=candidates[: args.limit],
        used_full_dataset=used_full_dataset,
    )


async def recent_activity(
    ctx: TurnContext, args: RecentActivityInput
) -> RecentActivityOutput:
    logger.info("read tool recent_activity turn_id=%s", ctx.turn_id)
    end = _ctx_now(ctx)
    start = end - timedelta(days=args.days)
    participant_ids = [ctx.user.id]
    if ctx.partner is not None:
        participant_ids.append(ctx.partner.id)
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
         AND m.bot_id = $3
         AND m.topic_id = $4
        WHERE u.id = ANY($5::uuid[])
        GROUP BY u.id, u.name
        ORDER BY last_message_at DESC NULLS LAST, u.name ASC
        """,
        start,
        end,
        ctx.bot_id,
        ctx.primary_topic_id,
        participant_ids,
    )
    threads: list[ThreadDigest] = []
    partner_share_by_user = await _partner_share_by_user_for_current_bot(ctx)
    for row in rows:
        count = int(value(row, "message_count", 0))
        partner_share = partner_share_by_user.get(row["user_id"])
        can_show_latest = raw_message_visibility(
            viewer_user_id=ctx.user.id,
            thread_owner_user_id=row["user_id"],
            thread_owner_partner_share=partner_share,
        ).visible
        snippet = (
            (value(row, "latest_content", "") or "")[:160] if can_show_latest else ""
        )
        # Plan 3 stub. tool_schemas.ThreadDigest.summary describes an LLM-generated digest; deferring the Haiku digest to Plan 4 alongside the significance scorer.
        if can_show_latest:
            summary = f'{count} messages this period; latest: "{snippet}"'
        else:
            summary = (
                f"{count} messages this period; latest content hidden by partner_share"
            )
        threads.append(
            ThreadDigest(
                user_id=row["user_id"],
                user_name=row["user_name"],
                message_count=count,
                last_message_at=row["last_message_at"],
                last_message_at_time=_time(row["last_message_at"], ctx),
                summary=summary,
            )
        )
    return RecentActivityOutput(
        threads=threads,
        period=DateRange(start=start, end=end),
        period_time={"start": _time(start, ctx), "end": _time(end, ctx)},
    )


def _message_thread_owner_id(row: Any) -> Any:
    if row["direction"] == "inbound" and row["sender_id"] is not None:
        return row["sender_id"]
    if row["direction"] == "outbound" and row["recipient_id"] is not None:
        return row["recipient_id"]
    return row["sender_id"] or row["recipient_id"]


async def list_themes(ctx: TurnContext, args: ListThemesInput) -> ListThemesOutput:
    logger.info("read tool list_themes turn_id=%s", ctx.turn_id)
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return ListThemesOutput(is_error=True, error=_err, themes=[])
    order_by = {
        "last_reinforced": "COALESCE(t.last_reinforced_at, t.first_seen_at) DESC",
        "last_active": "t.last_active_at DESC",
        "created": "t.first_seen_at DESC",
    }[args.sort_by.value]
    status_clause = "WHERE t.status = 'active'" if args.active_only else ""
    rows = await ctx.pool.fetch(
        f"""
        SELECT t.id, t.title, t.status, t.sentiment, t.health, t.last_reinforced_at, t.last_active_at
        FROM themes t
        {join_artifact_topics('t', '$2')}
        {status_clause}
        ORDER BY {order_by}, t.title ASC
        LIMIT $1
        """,
        args.limit,
        ctx.primary_topic_id,
    )
    return ListThemesOutput(
        themes=[
            theme_summary(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx))
            for row in rows
        ]
    )


async def get_theme(ctx: TurnContext, args: GetThemeInput) -> GetThemeOutput:
    logger.info("read tool get_theme turn_id=%s", ctx.turn_id)
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return GetThemeOutput(is_error=True, error=_err, theme=None)
    row = await ctx.pool.fetchrow(
        f"""
        SELECT t.id, t.title, t.description, t.status, t.sentiment, t.health, t.first_seen_at,
               t.last_reinforced_at, t.last_active_at
        FROM themes t
        {join_artifact_topics('t', '$2')}
        WHERE t.id = $1
        """,
        args.theme_id,
        ctx.primary_topic_id,
    )
    if row is None:
        return GetThemeOutput(theme=None)
    memory_rows = await ctx.pool.fetch(
        f"SELECT m.id FROM memories m {join_artifact_topics('m', '$2')} WHERE $1 = ANY(COALESCE(m.related_theme_ids, '{{}}'::uuid[]))",
        args.theme_id,
        ctx.primary_topic_id,
    )
    observation_rows = await ctx.pool.fetch(
        f"SELECT o.id FROM observations o {join_artifact_topics('o', '$2')} WHERE $1 = ANY(COALESCE(o.related_theme_ids, '{{}}'::uuid[]))",
        args.theme_id,
        ctx.primary_topic_id,
    )
    return GetThemeOutput(
        theme=ThemeDetail(
            **theme_summary(
                row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)
            ).model_dump(),
            description=row["description"],
            first_seen_at=row["first_seen_at"],
            first_seen_at_time=_time(row["first_seen_at"], ctx),
            related_memory_ids=[r["id"] for r in memory_rows],
            related_observation_ids=[r["id"] for r in observation_rows],
        )
    )


async def get_memories(ctx: TurnContext, args: GetMemoriesInput) -> GetMemoriesOutput:
    logger.info("read tool get_memories turn_id=%s", ctx.turn_id)
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return GetMemoriesOutput(is_error=True, error=_err, memories=[])
    clauses = ["m.status = $1"]
    params: list[Any] = [args.status.value]
    if args.couple_only:
        clauses.append("m.about_user_id IS NULL")
    elif args.about_user_id is not None:
        params.append(args.about_user_id)
        clauses.append(f"m.about_user_id = ${len(params)}")
    if args.theme_id is not None:
        params.append(args.theme_id)
        clauses.append(
            f"${len(params)} = ANY(COALESCE(m.related_theme_ids, '{{}}'::uuid[]))"
        )
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT m.id, m.about_user_id, m.content, m.status, m.visibility, m.shareable_summary,
               m.recorded_by_bot_id,
               COALESCE(m.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               m.created_at, m.last_referenced_at
        FROM memories m
        {join_artifact_topics('m', f'${len(params) + 1}')}
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(m.last_referenced_at, m.created_at) DESC
        LIMIT ${len(params)}
        """,
        *params,
        ctx.primary_topic_id,
    )
    partner_share_by_owner_bot: dict[tuple[Any, str], str] = {}
    visible_rows = []
    for row in rows:
        about_user_id = row["about_user_id"]
        if about_user_id is None or about_user_id == ctx.user.id:
            visible_rows.append(row)
            continue
        owner_partner_share = await _partner_share_for_owner_bot(
            ctx,
            partner_share_by_owner_bot,
            about_user_id,
            row["recorded_by_bot_id"] if "recorded_by_bot_id" in row else None,
        )
        if not raw_message_visibility(
            viewer_user_id=ctx.user.id,
            thread_owner_user_id=about_user_id,
            thread_owner_partner_share=owner_partner_share,
        ).visible:
            continue
        if row["visibility"] == "dyad_shareable" and row["shareable_summary"]:
            safe_row = dict(row)
            safe_row["content"] = row["shareable_summary"]
            visible_rows.append(safe_row)
    return GetMemoriesOutput(
        memories=[
            memory_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx))
            for row in visible_rows
        ]
    )


async def list_watch_items(
    ctx: TurnContext, args: ListWatchItemsInput
) -> ListWatchItemsOutput:
    logger.info("read tool list_watch_items turn_id=%s", ctx.turn_id)
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return ListWatchItemsOutput(is_error=True, error=_err, items=[])
    clauses: list[str] = []
    params: list[Any] = []
    if args.owner_user_id is not None:
        params.append(args.owner_user_id)
        clauses.append(f"w.owner_user_id = ${len(params)}")
    if args.status is not None:
        params.append(args.status.value)
        clauses.append(f"w.status = ${len(params)}")
    if args.due_before is not None:
        params.append(args.due_before)
        clauses.append(f"w.due_at <= ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await ctx.pool.fetch(
        f"""
        SELECT w.id, w.owner_user_id, w.content, w.due_at, w.status, w.addressing_note, w.created_at, w.addressed_at,
               COALESCE(w.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids
        FROM watch_items w
        {join_artifact_topics('w', f'${len(params) + 1}')}
        {where}
        ORDER BY COALESCE(w.due_at, w.created_at) ASC
        """,
        *params,
        ctx.primary_topic_id,
    )
    return ListWatchItemsOutput(
        items=[
            watch_item_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx))
            for row in rows
        ]
    )


async def get_observations(
    ctx: TurnContext, args: GetObservationsInput
) -> GetObservationsOutput:
    logger.info("read tool get_observations turn_id=%s", ctx.turn_id)
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return GetObservationsOutput(is_error=True, error=_err, observations=[])
    clauses = ["o.status = $1"]
    params: list[Any] = [args.status.value]
    if args.theme_id is not None:
        params.append(args.theme_id)
        clauses.append(
            f"${len(params)} = ANY(COALESCE(o.related_theme_ids, '{{}}'::uuid[]))"
        )
    if args.about_user_id is not None:
        params.append(args.about_user_id)
        clauses.append(f"o.about_user_id = ${len(params)}")
    if args.min_significance is not None:
        params.append(args.min_significance)
        clauses.append(f"o.significance >= ${len(params)}")
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT o.id, o.content, o.about_user_id, o.confidence, o.significance, o.status,
               COALESCE(o.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               COALESCE(o.supporting_message_ids, '{{}}'::uuid[]) AS supporting_message_ids,
               o.created_at, o.last_reinforced_at, o.surfaced_count
        FROM observations o
        {join_artifact_topics('o', f'${len(params) + 1}')}
        WHERE {' AND '.join(clauses)}
        ORDER BY recency_weighted_score(o.significance, o.last_reinforced_at, o.created_at) DESC NULLS LAST,
                 COALESCE(o.last_reinforced_at, o.created_at) DESC
        LIMIT ${len(params)}
        """,
        *params,
        ctx.primary_topic_id,
    )
    return GetObservationsOutput(
        observations=[
            observation_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx))
            for row in rows
        ]
    )


async def get_distillations(
    ctx: TurnContext, args: GetDistillationsInput
) -> GetDistillationsOutput:
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return GetDistillationsOutput(is_error=True, error=_err, distillations=[])
    logger.info("read tool get_distillations turn_id=%s", ctx.turn_id)
    clauses = ["d.status = $1"]
    params: list[Any] = [args.status.value]
    if args.source_user_id is not None:
        params.append(args.source_user_id)
        clauses.append(
            f"${len(params)} = ANY(COALESCE(d.source_user_ids, '{{}}'::uuid[]))"
        )
    if args.related_theme_id is not None:
        params.append(args.related_theme_id)
        clauses.append(
            f"${len(params)} = ANY(COALESCE(d.related_theme_ids, '{{}}'::uuid[]))"
        )
    if args.related_memory_id is not None:
        params.append(args.related_memory_id)
        clauses.append(
            f"${len(params)} = ANY(COALESCE(d.related_memory_ids, '{{}}'::uuid[]))"
        )
    if args.related_observation_id is not None:
        params.append(args.related_observation_id)
        clauses.append(
            f"${len(params)} = ANY(COALESCE(d.related_observation_ids, '{{}}'::uuid[]))"
        )
    if args.supporting_message_id is not None:
        params.append(args.supporting_message_id)
        clauses.append(
            f"${len(params)} = ANY(COALESCE(d.supporting_message_ids, '{{}}'::uuid[]))"
        )
    if args.text_contains:
        params.append(f"%{args.text_contains}%")
        clauses.append(
            f"""(
                d.content ILIKE ${len(params)}
                OR d.shareable_summary ILIKE ${len(params)}
                OR d.revision_note ILIKE ${len(params)}
            )"""
        )
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT d.id, d.content, d.confidence, d.status, d.sensitivity, d.visibility, d.shareable_summary,
               COALESCE(d.source_user_ids, '{{}}'::uuid[]) AS source_user_ids,
               COALESCE(d.related_memory_ids, '{{}}'::uuid[]) AS related_memory_ids,
               COALESCE(d.related_observation_ids, '{{}}'::uuid[]) AS related_observation_ids,
               COALESCE(d.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               COALESCE(d.supporting_message_ids, '{{}}'::uuid[]) AS supporting_message_ids,
               d.created_from_tool_call_id, d.triggering_message_id,
               d.supersedes_distillation_id, d.superseded_by_distillation_id,
               d.revision_note, d.revision_count,
               d.created_at, d.updated_at, d.revised_at, d.retired_at,
               d.recorded_by_bot_id, COALESCE(d.recorded_by_bot_id, tm.bot_id) AS visibility_bot_id
        FROM distillations d
        LEFT JOIN messages tm ON tm.id = d.triggering_message_id
        {join_artifact_topics('d', f'${len(params) + 1}')}
        WHERE {' AND '.join(clauses)}
        ORDER BY d.updated_at DESC, d.created_at DESC
        LIMIT ${len(params)}
        """,
        *params,
        ctx.primary_topic_id,
    )
    partner_share_by_owner_bot: dict[tuple[Any, str], str] = {}
    visible_rows = []
    for row in rows:
        source_user_ids = list(row["source_user_ids"] or [])
        visibility_bot_id = (
            row["visibility_bot_id"] if "visibility_bot_id" in row else None
        )
        if source_user_ids and all(
            source_user_id == ctx.user.id for source_user_id in source_user_ids
        ):
            visible_rows.append(row)
            continue
        partner_visible = bool(source_user_ids)
        for source_user_id in source_user_ids:
            owner_partner_share = await _partner_share_for_owner_bot(
                ctx, partner_share_by_owner_bot, source_user_id, visibility_bot_id
            )
            if not raw_message_visibility(
                viewer_user_id=ctx.user.id,
                thread_owner_user_id=source_user_id,
                thread_owner_partner_share=owner_partner_share,
            ).visible:
                partner_visible = False
                break
        if (
            partner_visible
            and row["visibility"] == "dyad_shareable"
            and row["shareable_summary"]
        ):
            safe_row = dict(row)
            safe_row["content"] = row["shareable_summary"]
            safe_row["revision_note"] = None
            visible_rows.append(safe_row)
    return GetDistillationsOutput(
        distillations=[
            distillation_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx))
            for row in visible_rows
        ]
    )


async def get_oob(ctx: TurnContext, args: GetOOBInput) -> GetOOBOutput:
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return GetOOBOutput(is_error=True, error=_err, entries=[])
    logger.info("read tool get_oob turn_id=%s", ctx.turn_id)
    clauses: list[str] = []
    params: list[Any] = []
    if args.owner_id is not None:
        params.append(args.owner_id)
        clauses.append(f"x.owner_id = ${len(params)}")
    if not args.include_lifted:
        clauses.append("x.status = 'active'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await ctx.pool.fetch(
        f"""
        SELECT x.id, x.owner_id, x.shareable_context, x.severity, x.status, x.created_at, x.review_at
        FROM out_of_bounds x
        {join_artifact_topics('x', f'${len(params) + 1}')}
        {where}
        ORDER BY x.created_at DESC
        """,
        *params,
        ctx.primary_topic_id,
    )
    return GetOOBOutput(
        entries=[
            oob_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)) for row in rows
        ]
    )


async def check_oob(ctx: TurnContext, args: CheckOOBInput) -> CheckOOBOutput:
    logger.info("read tool check_oob turn_id=%s", ctx.turn_id)
    return await check_oob_with_policy(
        ctx.pool,
        content=args.content,
        recipient_id=args.recipient_id,
        protected_owner_ids=args.protected_owner_ids,
        sender_intent=args.sender_intent,
        topic_id=ctx.primary_topic_id,
    )


async def summarize_oob_topics(
    ctx: TurnContext, args: SummarizeOOBTopicsInput
) -> SummarizeOOBTopicsOutput:
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return SummarizeOOBTopicsOutput(
            is_error=True, error=_err, total_count=0, clusters=[], narrative=""
        )
    logger.info("read tool summarize_oob_topics turn_id=%s", ctx.turn_id)
    return await summarize_partner_oob(
        ctx.pool, owner_id=args.owner_id, topic_id=ctx.primary_topic_id
    )


async def get_self_model(
    ctx: TurnContext, args: GetSelfModelInput
) -> GetSelfModelOutput:
    _err = check_read_scope(ctx, args.scope)
    if _err is not None:
        return GetSelfModelOutput(is_error=True, error=_err, model=None)
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
    watch_items = await list_watch_items(
        ctx, ListWatchItemsInput(owner_user_id=args.user_id)
    )
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
        "distillation": {
            "get_distillations",
            "add_distillation",
            "update_distillation",
            "revise_distillation",
        },
        "theme": {"create_theme", "update_theme"},
        "watch_item": {"add_watch_item", "update_watch_item", "address_watch_item"},
        "oob": {"add_oob", "update_oob", "lift_oob"},
        "schedule": {"schedule_checkin", "cancel_scheduled_checkin"},
        "escalation": {"escalate_to_partner"},
    }.get(value, set())


async def get_bot_actions(
    ctx: TurnContext, args: GetBotActionsInput
) -> GetBotActionsOutput:
    """Audit read: returns bot_turns enriched with triggering + outbound content.

    Backed by the SQL view ``mediator.v_bot_actions`` (migration 0043). The
    view denormalises ``bot_turns`` against ``messages``, ``tool_calls``, and
    ``turn_audit_events`` in a single place so that adding a new column never
    requires touching the application layer.

    Bot-scoping is enforced here as a mandatory ``bot_id = $N`` filter
    against the view (no opt-out flag — see Project B work item 3 and
    SD-014 on bot-scope discipline).
    """
    logger.info("read tool get_bot_actions turn_id=%s", ctx.turn_id)
    # bot_id is always set on the turn context; the view is defined so that
    # every row carries the originating bot, and we MUST scope to it.
    if ctx.bot_id is None:
        # Defensive: callers should never reach get_bot_actions without a
        # bot_id (the turn that invoked the tool is itself bot-scoped).
        return GetBotActionsOutput(actions=[])

    clauses: list[str] = ["bot_id = $1"]
    params: list[Any] = [ctx.bot_id]
    add_date_range(clauses, params, "started_at", args.date_range)
    if args.user_in_context is not None:
        params.append(args.user_in_context)
        clauses.append(f"user_in_context = ${len(params)}")
    target_names = _target_tool_names(args.target_type)
    if target_names:
        params.append(list(target_names))
        # tool_calls in the view is a jsonb array of tool_calls rows; check
        # whether any element's tool_name is in the target set.
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM jsonb_array_elements(tool_calls) tcf "
            f"WHERE tcf->>'tool_name' = ANY(${len(params)}::text[])"
            ")"
        )
    where = f"WHERE {' AND '.join(clauses)}"
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT turn_id, started_at, user_in_context, triggered_by_message_id,
               triggering_content, triggering_handling_result,
               triggering_processing_error,
               final_output_message_id, final_outbound_content,
               reasoning, tool_calls, audit_events
        FROM v_bot_actions
        {where}
        ORDER BY started_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return GetBotActionsOutput(
        actions=[
            BotAction(
                turn_id=row["turn_id"],
                started_at=row["started_at"],
                started_at_time=_time(row["started_at"], ctx),
                user_in_context=row["user_in_context"],
                triggered_by_message_id=row["triggered_by_message_id"],
                final_output_message_id=row["final_output_message_id"],
                triggering_content=row["triggering_content"],
                final_outbound_content=row["final_outbound_content"],
                reasoning=row["reasoning"],
                tool_calls=list(row["tool_calls"] or []),
                audit_events=_with_audit_event_times(
                    list(row.get("audit_events") or []), ctx
                ),
                handling_result=row.get("triggering_handling_result"),
                processing_error=row.get("triggering_processing_error"),
            )
            for row in rows
        ]
    )


async def get_tool_call(
    ctx: TurnContext, args: GetToolCallInput
) -> GetToolCallOutput:
    """Fetch full arguments + result for a single past tool call by id.

    Surfaces from the silent-turns hot-context block and from
    get_bot_actions tool_calls listings. Use when the highlight summary
    isn't specific enough.
    """
    logger.info(
        "read tool get_tool_call turn_id=%s target_tool_call_id=%s",
        ctx.turn_id,
        args.tool_call_id,
    )
    row = await ctx.pool.fetchrow(
        """
        SELECT id, turn_id, tool_name, kind, summary,
               arguments, result, called_at, duration_ms
        FROM tool_calls
        WHERE id = $1
        """,
        args.tool_call_id,
    )
    if row is None:
        return GetToolCallOutput(tool_call=None)
    return GetToolCallOutput(
        tool_call=ToolCallDetail(
            id=row["id"],
            turn_id=row["turn_id"],
            tool_name=row["tool_name"],
            kind=row["kind"],
            summary=row["summary"],
            arguments=dict(row["arguments"] or {}),
            result=dict(row["result"] or {}),
            called_at=row["called_at"],
            called_at_time=_time(row["called_at"], ctx),
            duration_ms=row["duration_ms"],
        )
    )


# NOTE (Critique flag 6): the symmetric write-tool `list_scheduled_tasks`
# lives in app/services/tools/write_tools.py:1840 by historical accident —
# it is a pure read but was placed in write_tools. We register
# `list_scheduled_checkins` here in read_tools.py (correct grouping) and
# leave a TODO to move list_scheduled_tasks in a follow-up PR.
# TODO(scheduling-cleanup): relocate list_scheduled_tasks from
# write_tools.py to this file for consistency.
async def list_scheduled_checkins(
    ctx: TurnContext, args: ListScheduledCheckinsInput
) -> ListScheduledCheckinsOutput:
    """Return this user's pending check-ins for the current bot.

    Scoped to ``ctx.user.id × ctx.bot_id`` (SD-014). A user with both
    mediator and Tante Rosi check-ins sees only the current bot's.
    """
    rows = await ctx.pool.fetch(
        """
        SELECT id AS job_id, bot_id, topic_id, scheduled_for, context, created_at
        FROM scheduled_jobs
        WHERE user_id=$1
          AND bot_id=$2
          AND job_type='checkin'
          AND status='pending'
        ORDER BY scheduled_for ASC
        LIMIT $3
        """,
        ctx.user.id,
        ctx.bot_id,
        args.limit,
    )
    timezone = ctx.user.timezone or "UTC"
    now = ctx.turn_started_at or datetime.now(UTC)
    checkins: list[ScheduledCheckinRow] = []
    for row in rows:
        context = row.get("context") if isinstance(row, dict) else row["context"]
        context = context or {}
        checkins.append(
            ScheduledCheckinRow(
                job_id=row["job_id"],
                bot_id=row.get("bot_id") if isinstance(row, dict) else row["bot_id"],
                topic_id=row.get("topic_id") if isinstance(row, dict) else row["topic_id"],
                scheduled_for=row["scheduled_for"],
                scheduled_for_time=temporal_reference(
                    row["scheduled_for"], timezone, now=now
                ),
                about_what=context.get("about_what"),
                reason=context.get("reason"),
                created_at=(
                    row.get("created_at")
                    if isinstance(row, dict)
                    else row["created_at"]
                    if "created_at" in row
                    else None
                ),
                created_at_time=temporal_reference(
                    (
                        row.get("created_at")
                        if isinstance(row, dict)
                        else row["created_at"]
                        if "created_at" in row
                        else None
                    ),
                    timezone,
                    now=now,
                ),
            )
        )
    return ListScheduledCheckinsOutput(checkins=checkins)


async def list_all_reminders(
    ctx: TurnContext, args: ListAllRemindersInput
) -> ListAllRemindersOutput:
    """Return a unified list of every pending agent-managed task AND user-facing
    check-in for the current ``(user_id, bot_id, topic_id)`` scope, ordered
    ascending by next fire time.

    Each item includes the ``scheduled_jobs.id``, a human-readable
    ``recurrence_label``, and the canonical ``recurrence_rule`` dict (pass it
    back verbatim to ``update_scheduled_task`` when changing recurrence).

    **Recurrence rule shape** (source of truth:
    ``app/services/scheduled_task_recurrence.py:49-87``, ``normalize_recurrence``):

    .. code-block::

        {
            "version": 1,
            "type": "daily" | "weekly" | "hourly",
            "interval": <positive int, default 1>,
            "weekdays": [<int 0=Mon..6=Sun>],        # weekly only, sorted
            "until": "<ISO8601 with tz>",             # optional
            "remaining_occurrences": <non-negative int>,  # optional
            "cancelled": true,                        # optional
        }

    One-off tasks (no recurrence rule) and all check-ins return
    ``recurrence_rule=None`` and ``recurrence_label='one-off'``.

    **Scoping divergence from** ``list_scheduled_checkins``:
    ``list_scheduled_checkins`` is scoped to ``(user_id, bot_id)``.
    ``list_all_reminders`` is scoped to ``(user_id, bot_id, topic_id)``.

    **cancel_scheduled_checkin asymmetry:**
    ``cancel_scheduled_checkin`` does NOT accept a ``job_id`` — it cancels
    at user scope only (``write_tools.py:1684-1702``).  Bots can see
    specific check-in IDs via this tool but cannot precision-cancel them.

    **schedule_checkin global supersession:**
    ``_schedule_once`` in ``app/services/checkins.py:33-41`` supersedes ALL
    pending check-ins for the user across all bots and topics.  This tool
    is scoped to ``(user, bot, topic)`` — a bot may see zero check-ins for
    its scope while ``schedule_checkin`` silently deletes check-ins from
    other bots.
    """
    rows = await ctx.pool.fetch(
        """
        SELECT id, job_type, scheduled_for, context
        FROM scheduled_jobs
        WHERE user_id=$1
          AND bot_id=$2
          AND topic_id=$3
          AND status='pending'
          AND job_type IN ('scheduled_task', 'checkin')
        ORDER BY scheduled_for ASC
        """,
        ctx.user.id,
        ctx.bot_id,
        ctx.primary_topic_id,
    )
    timezone = ctx.user.timezone or "UTC"
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    now = ctx.turn_started_at or datetime.now(UTC)
    items: list[ReminderItem] = []
    for row in rows:
        context = row.get("context") if isinstance(row, dict) else row["context"]
        context = context or {}
        scheduled_for = row["scheduled_for"]
        local_dt = scheduled_for.astimezone(tz) if hasattr(scheduled_for, 'astimezone') else scheduled_for
        tref = temporal_reference(scheduled_for, timezone, now=now)
        next_fire_local: str = tref.get("display", scheduled_for.isoformat()) if isinstance(tref, dict) else scheduled_for.isoformat()
        job_type: str = row["job_type"]
        kind: Literal["task", "checkin"] = (
            "task" if job_type == "scheduled_task" else "checkin"
        )
        if kind == "task":
            raw_recurrence = context.get("recurrence")
            try:
                recurrence_rule = normalize_recurrence(raw_recurrence)
            except Exception:
                # normalize_recurrence raises for unknown types; fall back
                # to the raw dict so the label generator can pretty-print it.
                recurrence_rule = dict(raw_recurrence) if isinstance(raw_recurrence, dict) else None
            recurrence_label = _format_recurrence_label(recurrence_rule, local_dt)
            items.append(
                ReminderItem(
                    id=row["id"],
                    kind=kind,
                    next_fire_local=next_fire_local,
                    next_fire_utc=scheduled_for,
                    recurrence_label=recurrence_label,
                    recurrence_rule=recurrence_rule,
                    brief=context.get("brief"),
                    about_what=None,
                    reason=None,
                )
            )
        else:
            items.append(
                ReminderItem(
                    id=row["id"],
                    kind=kind,
                    next_fire_local=next_fire_local,
                    next_fire_utc=scheduled_for,
                    recurrence_label="one-off",
                    recurrence_rule=None,
                    brief=None,
                    about_what=context.get("about_what"),
                    reason=context.get("reason"),
                )
            )
    return ListAllRemindersOutput(items=items)


def _format_recurrence_label(
    rule: dict[str, Any] | None, scheduled_for_local: Any
) -> str:
    """Derive a human-readable recurrence label from the canonical rule.

    The HH:MM portion comes from *scheduled_for_local* (the row's fire
    time cast to the user's local timezone), NOT from the rule.
    """
    HH_MM = ""
    if hasattr(scheduled_for_local, "strftime"):
        HH_MM = scheduled_for_local.strftime("%H:%M")

    if rule is None:
        return "one-off"

    rtype = rule.get("type", "")
    interval = rule.get("interval", 1)

    if rtype == "daily":
        if interval == 1:
            label = f"daily at {HH_MM} local"
        else:
            label = f"every {interval} days at {HH_MM} local"
        return label

    if rtype == "weekly":
        weekdays = rule.get("weekdays", [])
        short_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        names = [short_names[d % 7] for d in weekdays] if weekdays else []
        joined = "+".join(names) if names else "?"
        if interval == 1:
            label = f"weekly {joined} {HH_MM} local"
        else:
            label = f"every {interval} weeks {joined} {HH_MM} local"
        return label

    if rtype == "hourly":
        if interval == 1:
            return "hourly"
        return f"every {interval} hours"

    # Fallback: pretty-print the rule with local time
    return f"{rule!r} at {HH_MM} local"


# ── Hector fitness read tools ──────────────────────────────────────────────


async def list_commitments(
    ctx: TurnContext, args: "ListCommitmentsInput"
) -> "ListCommitmentsOutput":
    """List active or filtered commitments for the current user/topic/bot."""
    _check_hector_read_scope(ctx)

    conditions = [
        "user_id = $1",
        "topic_id = $2",
        "bot_id = $3",
    ]
    params: list[Any] = [ctx.user.id, ctx.primary_topic_id, ctx.bot_id]
    param_idx = 4

    if args.status is not None:
        conditions.append(f"status = ${param_idx}")
        params.append(args.status)
        param_idx += 1

    rows = await ctx.pool.fetch(
        f"""
        SELECT id, label, kind, status, cadence, days_of_week, target_count,
               start_date, end_date, pressure_style, created_at, updated_at
        FROM mediator.commitments
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC
        LIMIT 50
        """,
        *params,
    )

    commitments = [
        CommitmentSummary(
            id=str(row["id"]),
            label=row["label"],
            kind=row["kind"],
            status=row["status"],
            cadence=row["cadence"],
            days_of_week=list(row["days_of_week"]) if row["days_of_week"] else [],
            target_count=row["target_count"],
            start_date=row["start_date"].isoformat() if row["start_date"] else "",
            end_date=row["end_date"].isoformat() if row["end_date"] else None,
            pressure_style=row["pressure_style"],
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
            updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        )
        for row in rows
    ]

    return ListCommitmentsOutput(commitments=commitments)


async def list_events(
    ctx: TurnContext, args: "ListEventsInput"
) -> "ListEventsOutput":
    """List recent events for the current user/topic/bot, optionally filtered."""
    _check_hector_read_scope(ctx)

    conditions = [
        "user_id = $1",
        "topic_id = $2",
        "bot_id = $3",
    ]
    params: list[Any] = [ctx.user.id, ctx.primary_topic_id, ctx.bot_id]
    param_idx = 4

    if args.commitment_id is not None:
        from app.services.tools.common import parse_optional_uuid_field  # noqa: PLC0415

        _validated_cid = parse_optional_uuid_field(
            args.commitment_id,
            field_name="commitment_id",
            tool_name="list_events",
        )
        conditions.append(f"commitment_id = ${param_idx}::uuid")
        params.append(str(_validated_cid))
        param_idx += 1

    if args.before is not None:
        conditions.append(f"observed_at < ${param_idx}::timestamptz")
        params.append(args.before)
        param_idx += 1

    limit = args.limit or 20
    params.append(limit)

    rows = await ctx.pool.fetch(
        f"""
        SELECT id, commitment_id, metric_key, adherence_status,
               value_numeric, value_text, unit, observed_at, note, created_at
        FROM mediator.events
        WHERE {' AND '.join(conditions)}
        ORDER BY observed_at DESC
        LIMIT ${param_idx}
        """,
        *params,
    )

    events = [
        EventSummary(
            id=str(row["id"]),
            commitment_id=str(row["commitment_id"]) if row["commitment_id"] else None,
            metric_key=row["metric_key"],
            adherence_status=row["adherence_status"],
            value_numeric=float(row["value_numeric"]) if row["value_numeric"] is not None else None,
            value_text=row["value_text"],
            unit=row["unit"],
            observed_at=row["observed_at"].isoformat() if row["observed_at"] else "",
            note=row["note"],
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
        )
        for row in rows
    ]

    return ListEventsOutput(events=events)


async def get_adherence(
    ctx: TurnContext, args: "GetAdherenceInput"
) -> "GetAdherenceOutput":
    """Compute adherence checklist for active commitments."""
    from datetime import date as _date, datetime as _datetime

    _check_hector_read_scope(ctx)

    # Resolve timezone from user
    user_tz = ctx.user.timezone or "UTC"
    today = _date.today()

    # Query active commitments for this user/topic/bot
    conds = [
        "user_id = $1",
        "topic_id = $2",
        "bot_id = $3",
        "status = 'active'",
    ]
    params: list[Any] = [ctx.user.id, ctx.primary_topic_id, ctx.bot_id]
    param_idx = 4

    if args.commitment_ids:
        from app.services.tools.common import parse_optional_uuid_field  # noqa: PLC0415

        _validated_cids: list[str] = []
        for _cid in args.commitment_ids:
            _v = parse_optional_uuid_field(
                _cid,
                field_name="commitment_ids",
                tool_name="get_adherence",
            )
            _validated_cids.append(str(_v))
        conds.append(f"id = ANY(${param_idx}::uuid[])")
        params.append(_validated_cids)
        param_idx += 1

    crows = await ctx.pool.fetch(
        f"""
        SELECT id, label, cadence, days_of_week, target_count,
               start_date, end_date, schedule_rule
        FROM mediator.commitments
        WHERE {' AND '.join(conds)}
        ORDER BY created_at
        """,
        *params,
    )

    if not crows:
        return GetAdherenceOutput()

    # Build list of commitment IDs for event query
    cids = [row["id"] for row in crows]

    # Query events for the last 14 days
    from datetime import timedelta as _td
    cutoff = _datetime.now(UTC) - _td(days=14)

    erows = await ctx.pool.fetch(
        """
        SELECT id, commitment_id, adherence_status, value_numeric, value_text,
               observed_at
        FROM mediator.events
        WHERE user_id = $1
          AND topic_id = $2
          AND bot_id = $3
          AND observed_at >= $4
          AND commitment_id = ANY($5::uuid[])
        ORDER BY observed_at DESC
        """,
        ctx.user.id,
        ctx.primary_topic_id,
        ctx.bot_id,
        cutoff,
        cids,
    )

    # Group events by commitment_id
    events_by_cid: dict[str, list[dict[str, Any]]] = {}
    for row in erows:
        cid_key = str(row["commitment_id"]) if row["commitment_id"] else "_none"
        events_by_cid.setdefault(cid_key, []).append(dict(row))

    # Compute adherence per commitment
    from app.services.adherence import compute_adherence, summarize_board
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(user_tz)
    except Exception:
        from datetime import timezone as _tz
        tz = _tz.utc  # type: ignore[assignment]

    commitments: list[CommitmentAdherence] = []
    week_start: str | None = None
    week_end: str | None = None

    for crow in crows:
        cdict = dict(crow)
        cid_str = str(cdict["id"])
        evts = events_by_cid.get(cid_str, [])

        board = compute_adherence(cdict, evts, today, tz)

        if week_start is None and board.slots:
            week_start = board.slots[0].date.isoformat()
            week_end = board.slots[-1].date.isoformat()

        slots = [
            AdherenceSlot(
                date=s.date.isoformat(),
                day_label=s.day_label,
                status=s.status,
            )
            for s in board.slots
        ]

        summary = summarize_board(board)

        commitments.append(
            CommitmentAdherence(
                commitment_id=cid_str,
                label=board.label,
                cadence=board.cadence,
                slots=slots,
                summary=summary,
            )
        )

    return GetAdherenceOutput(
        commitments=commitments,
        week_start=week_start,
        week_end=week_end,
    )


_COMMITMENT_BOT_IDS: frozenset[str] = frozenset({"hector", "habits"})
_COMMITMENT_TOPIC_SLUGS: frozenset[str] = frozenset({"fitness", "habits"})


def _check_hector_read_scope(ctx: TurnContext) -> None:
    """Enforce that only commitment-tracking bots can call these read tools.

    Rejects if any scope value (bot_id, primary_topic_id, user.id) is None,
    or if the (bot_id, topic_slug) pair is not in the commitment-tracking
    set. Today: Hector on `fitness`, Habits on `habits`.
    """
    if ctx.bot_id is None:
        raise ValueError(
            "commitment/event read tools require ctx.bot_id (got None)"
        )
    if ctx.primary_topic_id is None:
        raise ValueError(
            "commitment/event read tools require ctx.primary_topic_id (got None)"
        )
    if ctx.user.id is None:
        raise ValueError(
            "commitment/event read tools require ctx.user.id (got None)"
        )
    if ctx.bot_id not in _COMMITMENT_BOT_IDS:
        raise ValueError(
            f"Commitment/event read tools are restricted to "
            f"{sorted(_COMMITMENT_BOT_IDS)}, got bot_id={ctx.bot_id!r}"
        )
    if ctx.primary_topic_slug not in _COMMITMENT_TOPIC_SLUGS:
        raise ValueError(
            f"Commitment/event read tools require a commitment topic "
            f"({sorted(_COMMITMENT_TOPIC_SLUGS)}), "
            f"got primary_topic_slug={ctx.primary_topic_slug!r}"
        )


# ── Conversation-plan read tools ───────────────────────────────────────────


class _DisplayProxy:
    """Minimal proxy so agenda_to_display() works without a full AgendaItem."""

    __slots__ = ("title",)

    def __init__(self, title: str) -> None:
        self.title = title


async def read_conversation_plan(
    ctx: TurnContext, args: ReadConversationPlanInput
) -> ReadConversationPlanOutput:
    """Return the agenda items for a single conversation owned by the caller."""
    from app.services.tools.write_tools import ToolCallRejected  # noqa: PLC0415

    row = await ctx.pool.fetchrow(
        """
        SELECT id, status, mode, current_item_id
        FROM mediator.conversations
        WHERE id=$1 AND user_id=$2
        """,
        args.conversation_id,
        ctx.user.id,
    )
    if row is None:
        raise ToolCallRejected({"error": "not found or not owned"})

    item_rows = await ctx.pool.fetch(
        """
        SELECT id, title, priority, order_hint
        FROM mediator.conversation_items
        WHERE conversation_id=$1 AND kind='planned'
        ORDER BY order_hint
        """,
        args.conversation_id,
    )

    items = [
        PlanItem(
            id=r["id"],
            title=r["title"],
            priority=r["priority"],
            order_hint=r["order_hint"],
        )
        for r in item_rows
    ]

    proxies = [_DisplayProxy(item.title) for item in items]
    display_text = agenda_to_display(proxies)  # type: ignore[arg-type]

    return ReadConversationPlanOutput(
        conversation_id=args.conversation_id,
        status=row["status"],
        items=items,
        display_text=display_text,
    )


async def list_conversation_plans(
    ctx: TurnContext, args: ListConversationPlansInput
) -> ListConversationPlansOutput:
    """Return a summary list of the caller's conversation plans."""
    rows = await ctx.pool.fetch(
        """
        SELECT
            c.id, c.status, c.started_at, c.created_at,
            (SELECT ci.title FROM mediator.conversation_items ci
             WHERE ci.conversation_id = c.id AND ci.kind='planned'
             ORDER BY ci.order_hint LIMIT 1) AS first_title,
            (SELECT COUNT(*) FROM mediator.conversation_items ci
             WHERE ci.conversation_id = c.id AND ci.kind='planned') AS item_count
        FROM mediator.conversations c
        WHERE c.user_id=$1 AND c.status IN ('prepping','preparing','ready')
        ORDER BY c.started_at DESC
        LIMIT $2
        """,
        ctx.user.id,
        args.limit,
    )

    plans = [
        ListConversationPlansRow(
            conversation_id=r["id"],
            status=r["status"],
            title=r["first_title"] or "Untitled",
            item_count=int(r["item_count"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]

    return ListConversationPlansOutput(plans=plans)
