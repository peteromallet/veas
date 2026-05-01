"""FastAPI application entrypoint."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.config import get_settings
from app.db import db_lifespan
from app.routers import admin, health, whatsapp as whatsapp_router
from app.services import agentic, discord, hooks, whatsapp
from app.services.agentic import run_agentic_turn
from app.services.debouncer import BurstCoalescer
from app.services.recovery import recover_on_startup, run_recovery_forever
from app.services.scheduled_job_handlers import ScheduledJobHandlers, seed_weekly_summaries
from app.services.scheduled_jobs import ScheduledJobWorker, seed_heartbeat

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with db_lifespan(app):
        settings = get_settings()
        pool = app.state.pool
        if settings.messaging_provider.strip().lower() == "discord":
            await discord.init_client()
            await discord.seed_partner_users(pool)
        else:
            await whatsapp.init_client()
        agentic.set_pool(pool)
        hooks.set_pool(pool)
        app.state.coalescer = BurstCoalescer(on_burst_complete=run_agentic_turn)
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
