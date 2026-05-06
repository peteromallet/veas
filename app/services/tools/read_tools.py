"""Read-only tools for the agentic loop."""

from __future__ import annotations

import logging
import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.cross_thread_privacy import bridge_candidate_visible_to_target, normalize_sharing_default, raw_message_visibility
from app.services.turn_context import TurnContext
from app.config import get_settings
from app.services.messaging import send_outbound_part
from app.services.oob_check import check_oob_with_policy, summarize_partner_oob
from app.services.text_safety import clean_user_facing_text, looks_like_internal_process_text
from app.services.time_context import local_day_bounds_utc, temporal_reference
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
    GetThemeInput,
    GetThemeOutput,
    ListBridgeCandidatesInput,
    ListBridgeCandidatesOutput,
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
    SelfModel,
    SendMessagePartInput,
    SendMessagePartOutput,
    SummarizeOOBTopicsInput,
    SummarizeOOBTopicsOutput,
    ThemeDetail,
    ThreadDigest,
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


def _ctx_timezone(ctx: TurnContext, override: str | None = None) -> str:
    return override or ctx.user.timezone or "UTC"


def _ctx_now(ctx: TurnContext) -> datetime:
    return ctx.turn_started_at or datetime.now(UTC)


def _time(value_: datetime | None, ctx: TurnContext, *, timezone: str | None = None) -> dict[str, str] | None:
    return temporal_reference(value_, _ctx_timezone(ctx, timezone), now=_ctx_now(ctx))


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


def _emoji_score(query_terms: set[str], name: str, aliases: list[str], keywords: list[str]) -> int:
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
            )
            """,
            ctx.user.id,
            boundary,
            ctx.triggering_message_ids,
        )
    )


async def send_message_part(ctx: TurnContext, args: SendMessagePartInput) -> SendMessagePartOutput:
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
    if looks_like_internal_process_text(content) or clean_user_facing_text(content).strip() == "":
        return SendMessagePartOutput(
            status="withheld",
            client_part_key=args.client_part_key,
            visible_to_user=False,
            sent_so_far=[part["content"] for part in sent_parts],
            reason="content looks like internal process narration (memory IDs, write plans, phase notes); send a user-facing reply instead",
        )
    part_index = len(sent_parts) + 1
    part_key = f"{ctx.turn_id}:{part_index}"
    paced_send_available = ctx.before_paced_send is not None and not ctx.send_typing_indicator
    if sent_parts and settings.discord_multi_message_delay_s > 0 and not paced_send_available:
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

        async def before_provider_send(text: str = content, kind: str = send_kind, index: int = part_index) -> None:
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
    if output.visible_to_user and output.message_id is not None and output.delivered_content:
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


async def search_messages(ctx: TurnContext, args: SearchMessagesInput) -> SearchMessagesOutput:
    logger.info("read tool search_messages turn_id=%s", ctx.turn_id)
    dyad_ids = {ctx.user.id, ctx.partner.id}
    if args.partner_user_id is not None and args.partner_user_id not in dyad_ids:
        return SearchMessagesOutput(hits=[], truncated=False)
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if args.partner_user_id is not None:
        params.append(args.partner_user_id)
        clauses.append(f"(sender_id = ${len(params)} OR recipient_id = ${len(params)})")
    else:
        params.append([ctx.user.id, ctx.partner.id])
        clauses.append(f"(sender_id = ANY(${len(params)}::uuid[]) OR recipient_id = ANY(${len(params)}::uuid[]))")
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
        start, end = local_day_bounds_utc(args.local_day, _ctx_timezone(ctx, args.timezone), now=_ctx_now(ctx))
        params.extend([start, end])
        clauses.append(f"sent_at >= ${len(params) - 1}")
        clauses.append(f"sent_at < ${len(params)}")
    else:
        add_date_range(clauses, params, "sent_at", args.date_range)
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, sender_id, recipient_id, sent_at, content, media_type, media_analysis,
               COALESCE(charge, 'routine') AS charge, direction
        FROM messages
        WHERE {' AND '.join(clauses)}
        ORDER BY sent_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    sharing_defaults = {
        ctx.user.id: normalize_sharing_default(ctx.user.cross_thread_sharing_default),
        ctx.partner.id: normalize_sharing_default(ctx.partner.cross_thread_sharing_default),
    }
    hits = []
    for row in rows:
        owner_id = _message_thread_owner_id(row)
        if owner_id not in dyad_ids:
            continue
        if not raw_message_visibility(
            viewer_user_id=ctx.user.id,
            thread_owner_user_id=owner_id,
            thread_owner_sharing_default=sharing_defaults.get(owner_id),
        ).visible:
            continue
        hits.append(message_hit(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)))
    return SearchMessagesOutput(hits=hits, truncated=len(rows) == args.limit)


async def list_bridge_candidates(ctx: TurnContext, args: ListBridgeCandidatesInput) -> ListBridgeCandidatesOutput:
    logger.info("read tool list_bridge_candidates turn_id=%s", ctx.turn_id)
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
        if row["target_user_id"] == ctx.user.id and row["source_user_id"] != ctx.user.id:
            if not bridge_candidate_visible_to_target(row, target_user_id=ctx.user.id):
                continue
            row = {**dict(row), "internal_note": None}
        candidates.append(_bridge_candidate(row))
    return ListBridgeCandidatesOutput(candidates=candidates, truncated=len(rows) == args.limit)


def _bridge_candidate(row: Any) -> BridgeCandidate:
    data = dict(row)
    data.setdefault("partner_path", "message_partner")
    data["source_message_ids"] = list(data.get("source_message_ids") or [])
    data["related_memory_ids"] = list(data.get("related_memory_ids") or [])
    data["related_observation_ids"] = list(data.get("related_observation_ids") or [])
    return BridgeCandidate.model_validate(data)


async def search_emojis(ctx: TurnContext, args: SearchEmojisInput) -> SearchEmojisOutput:
    logger.info("read tool search_emojis turn_id=%s", ctx.turn_id)
    query_terms = _emoji_terms(args.query)
    candidates: list[EmojiSearchHit] = []
    used_full_dataset = False

    try:
        import emoji as emoji_pkg  # type: ignore

        used_full_dataset = True
        for symbol, data in emoji_pkg.EMOJI_DATA.items():
            raw_name = str(data.get("en") or "").strip(":").replace("_", " ")
            aliases = [str(item).strip(":").replace("_", " ") for item in data.get("alias", []) or []]
            keywords = [str(item).replace("_", " ") for item in data.get("variant", []) or []]
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
    return SearchEmojisOutput(query=args.query, hits=candidates[: args.limit], used_full_dataset=used_full_dataset)


async def recent_activity(ctx: TurnContext, args: RecentActivityInput) -> RecentActivityOutput:
    logger.info("read tool recent_activity turn_id=%s", ctx.turn_id)
    end = _ctx_now(ctx)
    start = end - timedelta(days=args.days)
    rows = await ctx.pool.fetch(
        """
        SELECT u.id AS user_id, u.name AS user_name, u.cross_thread_sharing_default, COUNT(m.id) AS message_count,
               MAX(m.sent_at) AS last_message_at,
               (ARRAY_AGG(m.content ORDER BY m.sent_at DESC))[1] AS latest_content
        FROM users u
        LEFT JOIN messages m
          ON (m.sender_id = u.id OR m.recipient_id = u.id)
         AND m.sent_at >= $1
         AND m.sent_at <= $2
         AND m.deleted_at IS NULL
        WHERE u.id = ANY($3::uuid[])
        GROUP BY u.id, u.name, u.cross_thread_sharing_default
        ORDER BY last_message_at DESC NULLS LAST, u.name ASC
        """,
        start,
        end,
        [ctx.user.id, ctx.partner.id],
    )
    threads: list[ThreadDigest] = []
    for row in rows:
        count = int(value(row, "message_count", 0))
        sharing_default = value(row, "cross_thread_sharing_default", None)
        if row["user_id"] == ctx.user.id:
            sharing_default = ctx.user.cross_thread_sharing_default
        elif row["user_id"] == ctx.partner.id:
            sharing_default = ctx.partner.cross_thread_sharing_default
        can_show_latest = raw_message_visibility(
            viewer_user_id=ctx.user.id,
            thread_owner_user_id=row["user_id"],
            thread_owner_sharing_default=sharing_default,
        ).visible
        snippet = (value(row, "latest_content", "") or "")[:160] if can_show_latest else ""
        # Plan 3 stub. tool_schemas.ThreadDigest.summary describes an LLM-generated digest; deferring the Haiku digest to Plan 4 alongside the significance scorer.
        if can_show_latest:
            summary = f'{count} messages this period; latest: "{snippet}"'
        else:
            summary = f"{count} messages this period; latest content hidden by sharing_default"
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
    return ListThemesOutput(themes=[theme_summary(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)) for row in rows])


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
            **theme_summary(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)).model_dump(),
            description=row["description"],
            first_seen_at=row["first_seen_at"],
            first_seen_at_time=_time(row["first_seen_at"], ctx),
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
    return GetMemoriesOutput(memories=[memory_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)) for row in rows])


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
    return ListWatchItemsOutput(items=[watch_item_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)) for row in rows])


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
    return GetObservationsOutput(observations=[observation_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)) for row in rows])


async def get_distillations(ctx: TurnContext, args: GetDistillationsInput) -> GetDistillationsOutput:
    logger.info("read tool get_distillations turn_id=%s", ctx.turn_id)
    clauses = ["status = $1"]
    params: list[Any] = [args.status.value]
    if args.source_user_id is not None:
        params.append(args.source_user_id)
        clauses.append(f"${len(params)} = ANY(COALESCE(source_user_ids, '{{}}'::uuid[]))")
    if args.related_theme_id is not None:
        params.append(args.related_theme_id)
        clauses.append(f"${len(params)} = ANY(COALESCE(related_theme_ids, '{{}}'::uuid[]))")
    if args.related_memory_id is not None:
        params.append(args.related_memory_id)
        clauses.append(f"${len(params)} = ANY(COALESCE(related_memory_ids, '{{}}'::uuid[]))")
    if args.related_observation_id is not None:
        params.append(args.related_observation_id)
        clauses.append(f"${len(params)} = ANY(COALESCE(related_observation_ids, '{{}}'::uuid[]))")
    if args.supporting_message_id is not None:
        params.append(args.supporting_message_id)
        clauses.append(f"${len(params)} = ANY(COALESCE(supporting_message_ids, '{{}}'::uuid[]))")
    if args.text_contains:
        params.append(f"%{args.text_contains}%")
        clauses.append(
            f"""(
                content ILIKE ${len(params)}
                OR shareable_summary ILIKE ${len(params)}
                OR revision_note ILIKE ${len(params)}
            )"""
        )
    params.append(args.limit)
    rows = await ctx.pool.fetch(
        f"""
        SELECT id, content, confidence, status, sensitivity, visibility, shareable_summary,
               COALESCE(source_user_ids, '{{}}'::uuid[]) AS source_user_ids,
               COALESCE(related_memory_ids, '{{}}'::uuid[]) AS related_memory_ids,
               COALESCE(related_observation_ids, '{{}}'::uuid[]) AS related_observation_ids,
               COALESCE(related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               COALESCE(supporting_message_ids, '{{}}'::uuid[]) AS supporting_message_ids,
               created_from_tool_call_id, triggering_message_id,
               supersedes_distillation_id, superseded_by_distillation_id,
               revision_note, revision_count,
               created_at, updated_at, revised_at, retired_at
        FROM distillations
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC, created_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    sharing_defaults = {
        ctx.user.id: normalize_sharing_default(ctx.user.cross_thread_sharing_default),
        ctx.partner.id: normalize_sharing_default(ctx.partner.cross_thread_sharing_default),
    }
    visible_rows = []
    for row in rows:
        source_user_ids = list(row["source_user_ids"] or [])
        full_visible = bool(source_user_ids) and all(
            raw_message_visibility(
                viewer_user_id=ctx.user.id,
                thread_owner_user_id=source_user_id,
                thread_owner_sharing_default=sharing_defaults.get(source_user_id),
            ).visible
            for source_user_id in source_user_ids
        )
        if full_visible:
            visible_rows.append(row)
            continue
        if row["visibility"] == "dyad_shareable" and row["shareable_summary"]:
            safe_row = dict(row)
            safe_row["content"] = row["shareable_summary"]
            safe_row["revision_note"] = None
            visible_rows.append(safe_row)
    return GetDistillationsOutput(
        distillations=[distillation_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)) for row in visible_rows]
    )


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
    return GetOOBOutput(entries=[oob_row(row, timezone=_ctx_timezone(ctx), now=_ctx_now(ctx)) for row in rows])


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
        "distillation": {"get_distillations", "add_distillation", "update_distillation", "revise_distillation"},
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
               ) AS tool_calls,
               COALESCE(
                 (
                   SELECT jsonb_agg(
                     jsonb_build_object(
                       'id', tae.id,
                       'turn_id', tae.turn_id,
                       'event_seq', tae.event_seq,
                       'event_type', tae.event_type,
                       'step', tae.step,
                       'severity', tae.severity,
                       'occurred_at', tae.occurred_at,
                       'duration_ms', tae.duration_ms,
                       'actor', tae.actor,
                       'message', tae.message,
                       'metadata', tae.metadata
                     )
                     ORDER BY tae.event_seq
                   )
                   FROM turn_audit_events tae
                   WHERE tae.turn_id = bt.id
                 ),
                 '[]'::jsonb
               ) AS audit_events
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
                started_at_time=_time(row["started_at"], ctx),
                user_in_context=row["user_in_context"],
                triggered_by_message_id=row["triggered_by_message_id"],
                final_output_message_id=row["final_output_message_id"],
                triggering_content=row["triggering_content"],
                final_outbound_content=row["final_outbound_content"],
                reasoning=row["reasoning"],
                tool_calls=list(row["tool_calls"] or []),
                audit_events=_with_audit_event_times(list(row.get("audit_events") or []), ctx),
            )
            for row in rows
        ]
    )
