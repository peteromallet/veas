"""FastAPI application entrypoint."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from resident_chat_runtime.diagnostics import build_startup_diagnostics
from resident_chat_runtime.env import EnvSetting, read_env_settings

from app.config import Settings, get_settings
from app.db import db_lifespan
from app.models.user import User
from app.routers import admin, health, whatsapp as whatsapp_router
from app.services import agentic, discord, hooks, whatsapp
from app.services.agentic import run_agentic_turn, run_agentic_turn_with_metadata
from app.services.debouncer import BurstCoalescer
from app.services.pacer import DiscordPacer, PacedSendKind, PacingDecision
from app.services.recovery import recover_on_startup, run_recovery_forever
from app.services.scheduled_job_handlers import ScheduledJobHandlers, seed_weekly_summaries
from app.services.scheduled_jobs import ScheduledJobWorker, seed_heartbeat

logger = logging.getLogger(__name__)


def _log_startup_diagnostics() -> None:
    _, env_statuses = read_env_settings(
        [
            EnvSetting("MESSAGING_PROVIDER", default="whatsapp"),
            EnvSetting("DISCORD_BOT_TOKEN", secret=True),
            EnvSetting("DISCORD_PARTNER_USER_ID_A"),
            EnvSetting("DISCORD_PARTNER_USER_ID_B"),
        ]
    )
    for item in build_startup_diagnostics(env=env_statuses):
        logger.info("startup diagnostic %s=%s", item.name, item.detail)


def _discord_provider_enabled(settings: Settings) -> bool:
    return settings.messaging_provider.strip().lower() == "discord"


async def _run_paced_agentic_turn(
    message_ids: list[UUID],
    user: User,
    decision: PacingDecision,
    *,
    pacer: DiscordPacer | None = None,
) -> None:
    before_paced_send = None
    thinking_typing_stop: asyncio.Event | None = None
    thinking_typing_task: asyncio.Task[None] | None = None
    channel_id: str | None = None

    async def stop_thinking_typing() -> None:
        if thinking_typing_stop is None or thinking_typing_task is None:
            return
        thinking_typing_stop.set()
        try:
            await thinking_typing_task
        except Exception:
            logger.warning("paced thinking typing task failed", exc_info=True)

    if pacer is not None and decision.signal_snapshot.get("source") == "live":
        try:
            channel_id = await discord.get_dm_channel_id(user.phone)
            thinking_typing_stop = asyncio.Event()
            thinking_typing_task = asyncio.create_task(
                pacer.perform_thinking_typing_until_stopped(user, channel_id, thinking_typing_stop)
            )
            await asyncio.sleep(0)
        except Exception:
            logger.warning("failed to start paced thinking typing", exc_info=True)

        async def before_paced_send(
            answer_text: str,
            *,
            send_kind: PacedSendKind = "final",
            part_index: int | None = None,
        ) -> None:
            nonlocal channel_id
            await stop_thinking_typing()
            if channel_id is None:
                channel_id = await discord.get_dm_channel_id(user.phone)
            await pacer.perform_send_typing(user, channel_id, answer_text, send_kind=send_kind, part_index=part_index)

    try:
        await run_agentic_turn_with_metadata(
            message_ids,
            user,
            pacing_context=decision,
            before_paced_send=before_paced_send,
        )
    finally:
        await stop_thinking_typing()


async def _send_paced_reaction(pool: Any, message_ids: list[UUID], user: User, decision: PacingDecision) -> None:
    if not message_ids or not decision.reaction:
        return
    row = await pool.fetchrow(
        """
        SELECT whatsapp_message_id
        FROM messages
        WHERE id=$1 AND direction='inbound' AND sender_id=$2
        """,
        message_ids[-1],
        user.id,
    )
    if row is None or not row.get("whatsapp_message_id"):
        return
    await discord.add_reaction(user.phone, row["whatsapp_message_id"], decision.reaction)


def _build_coalescer(pool: Any, settings: Settings) -> tuple[BurstCoalescer, DiscordPacer | None]:
    if _discord_provider_enabled(settings) and settings.discord_pacing_enabled:
        pacer = DiscordPacer(pool, settings=settings, send_typing=discord.send_typing)

        async def on_live_typing(user: User, stop_event: asyncio.Event) -> None:
            channel_id = await discord.get_dm_channel_id(user.phone)
            await pacer.perform_initial_typing_until_stopped(user, channel_id, stop_event)

        async def on_paced_reaction(message_ids: list[UUID], user: User, decision: PacingDecision) -> None:
            await _send_paced_reaction(pool, message_ids, user, decision)

        return (
            BurstCoalescer(
                on_burst_complete=run_agentic_turn,
                debounce_seconds=settings.discord_pacing_burst_window_s,
                max_seconds=max(settings.discord_pacing_burst_window_s, settings.discord_pacing_max_wait_s),
                pacer=pacer,
                on_paced_answer=lambda message_ids, user, decision: _run_paced_agentic_turn(
                    message_ids,
                    user,
                    decision,
                    pacer=pacer,
                ),
                on_paced_reaction=on_paced_reaction,
                on_live_typing=on_live_typing,
            ),
            pacer,
        )
    return BurstCoalescer(on_burst_complete=run_agentic_turn), None


def _configure_coalescer(app: FastAPI, pool: Any, settings: Settings) -> None:
    coalescer, pacer = _build_coalescer(pool, settings)
    app.state.coalescer = coalescer
    app.state.discord_pacer = pacer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with db_lifespan(app):
        settings = get_settings()
        _log_startup_diagnostics()
        pool = app.state.pool
        if settings.messaging_provider.strip().lower() == "discord":
            await discord.init_client()
            await discord.seed_partner_users(pool)
        else:
            await whatsapp.init_client()
        agentic.set_pool(pool)
        hooks.set_pool(pool)
        _configure_coalescer(app, pool, settings)
        app.state.background_tasks: set[asyncio.Task] = set()
        await recover_on_startup(pool, app.state.coalescer)
        recovery_task = asyncio.create_task(run_recovery_forever(pool, app.state.coalescer))
        app.state.background_tasks.add(recovery_task)
        if settings.messaging_provider.strip().lower() == "discord":
            await discord.catch_up_recent_messages(pool, app.state.coalescer)
            discord_bot = discord.DiscordGatewayBot(pool, app.state.coalescer)
            app.state.discord_bot = discord_bot
            discord_task = asyncio.create_task(discord_bot.run_forever())
            app.state.background_tasks.add(discord_task)
        if settings.scheduler_enabled:
            await seed_heartbeat(pool, settings=settings)
            await seed_weekly_summaries(pool)
            worker = ScheduledJobWorker(
                pool,
                settings=settings,
                handlers=ScheduledJobHandlers(pool, settings=settings).as_dict(),
            )
            scheduler_task = asyncio.create_task(worker.run_forever())
            app.state.scheduler_worker = worker
            app.state.background_tasks.add(scheduler_task)
        try:
            yield
        finally:
            for task in list(app.state.background_tasks):
                task.cancel()
            for task in list(app.state.background_tasks):
                with suppress(asyncio.CancelledError):
                    await task
            discord_bot = getattr(app.state, "discord_bot", None)
            if discord_bot is not None:
                await discord_bot.close()
            await whatsapp.close_client()
            await discord.close_client()
            hooks.set_pool(None)


app = FastAPI(lifespan=lifespan)
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(whatsapp_router.router)
