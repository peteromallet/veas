"""FastAPI application entrypoint."""

import asyncio
import logging
import sys
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
from app.services.scheduled_job_handlers import ScheduledJobHandlers, seed_weekly_reflections
from app.services.scheduled_jobs import ScheduledJobWorker, seed_heartbeat

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Uvicorn installs its own handlers; don't double-log through root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = False


_configure_logging()


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

# ── Per-bot Discord token diagnostics ─────────────────────────────
    settings = get_settings()
    per_bot_tokens = settings.discord_bot_tokens
    legacy_token = settings.discord_bot_token
    overrides = settings.discord_bot_user_id_overrides

    configured_ids = sorted(per_bot_tokens)
    if configured_ids:
        logger.info(
            "startup diagnostic discord_configured_bot_ids=%s "
            "(tokens provided via DISCORD_BOT_TOKEN_<BOT_ID>)",
            ",".join(configured_ids),
        )
    else:
        logger.info("startup diagnostic discord_configured_bot_ids=none")

    if legacy_token and legacy_token.get_secret_value():
        logger.info(
            "startup diagnostic discord_legacy_token=present "
            "(single DISCORD_BOT_TOKEN set — will fall back for mediator "
            "when no per-bot token matches)"
        )
    else:
        logger.info("startup diagnostic discord_legacy_token=absent")

    # Log which bot_ids have *no* token (configured via overrides but no token)
    override_ids = sorted(overrides)
    if override_ids:
        logger.info(
            "startup diagnostic discord_bot_user_id_overrides=%s "
            "(DISCORD_BOT_USER_ID_<BOT_ID> env vars)",
            ",".join(override_ids),
        )
        missing = [bid for bid in override_ids if bid not in per_bot_tokens]
        if missing:
            logger.info(
                "startup diagnostic discord_bot_ids_missing_token=%s "
                "(have user-id override but no per-bot token — will be "
                "skipped unless legacy DISCORD_BOT_TOKEN applies)",
                ",".join(sorted(missing)),
            )


def _discord_provider_enabled(settings: Settings) -> bool:
    return settings.messaging_provider.strip().lower() == "discord"


def _make_send_typing(bot_id: str):
    """Return an async callable that sends a typing indicator as *bot_id*."""

    async def _send_typing(channel_id: str) -> None:
        await discord.send_typing(channel_id, bot_id=bot_id)

    return _send_typing


async def _run_paced_agentic_turn(
    message_ids: list[UUID],
    user: User,
    decision: PacingDecision,
    *,
    pacer: DiscordPacer | None = None,
    bot_id: str = "mediator",
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
            channel_id = await discord.get_dm_channel_id(user.phone, bot_id=bot_id)
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
                channel_id = await discord.get_dm_channel_id(user.phone, bot_id=bot_id)
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


async def _send_paced_reaction(pool: Any, message_ids: list[UUID], user: User, decision: PacingDecision, *, bot_id: str) -> None:
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
    await discord.add_reaction(user.phone, row["whatsapp_message_id"], decision.reaction, bot_id=bot_id)


def _build_coalescer_for_bot(pool: Any, settings: Settings, *, bot_id: str) -> tuple[BurstCoalescer, DiscordPacer | None]:
    """Build a per-bot coalescer + pacer pair.

    Each bot gets its own DiscordPacer (keyed by bot_id) and glue
    closures that capture that bot_id so outbound Discord calls
    (get_dm_channel_id, add_reaction, send_typing) route through the
    correct DiscordClient.
    """
    if _discord_provider_enabled(settings) and settings.discord_pacing_enabled:
        pacer = DiscordPacer(pool, settings=settings, send_typing=_make_send_typing(bot_id))

        async def on_live_typing(user: User, stop_event: asyncio.Event) -> None:
            channel_id = await discord.get_dm_channel_id(user.phone, bot_id=bot_id)
            await pacer.perform_initial_typing_until_stopped(user, channel_id, stop_event)

        async def on_paced_reaction(message_ids: list[UUID], user: User, decision: PacingDecision) -> None:
            await _send_paced_reaction(pool, message_ids, user, decision, bot_id=bot_id)

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
                    bot_id=bot_id,
                ),
                on_paced_reaction=on_paced_reaction,
                on_live_typing=on_live_typing,
            ),
            pacer,
        )
    return BurstCoalescer(on_burst_complete=run_agentic_turn), None


def _configure_coalescer(app: FastAPI, pool: Any, settings: Settings) -> None:
    coalescer, pacer = _build_coalescer_for_bot(pool, settings, bot_id="mediator")
    app.state.coalescer = coalescer
    app.state.discord_pacers: dict[str, DiscordPacer] = {}
    if pacer is not None:
        app.state.discord_pacers["mediator"] = pacer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with db_lifespan(app):
        settings = get_settings()
        _log_startup_diagnostics()
        pool = app.state.pool
        if settings.messaging_provider.strip().lower() == "discord":
            pass  # Discord clients are initialized per-bot below
        else:
            await whatsapp.init_client()
        agentic.set_pool(pool)
        hooks.set_pool(pool)
        # Sprint 1: populate mediator spec from DB (display_name only; rest hardcoded)
        from app.bots.registry import populate_mediator_spec_from_db

        await populate_mediator_spec_from_db(pool)
        # Sprint 7: check for Tante Rosi bots row (prod-registration gate)
        from app.bots.registry import populate_tante_rosi_spec_from_db

        await populate_tante_rosi_spec_from_db(pool)
        # Sprint 2a: cache relationship topic id for scope fallbacks
        from app.bots.registry import populate_topic_ids_from_db

        await populate_topic_ids_from_db(pool)
        _configure_coalescer(app, pool, settings)
        app.state.background_tasks: set[asyncio.Task] = set()
        await recover_on_startup(pool, app.state.coalescer)
        recovery_task = asyncio.create_task(run_recovery_forever(pool, app.state.coalescer))
        app.state.background_tasks.add(recovery_task)
        if settings.messaging_provider.strip().lower() == "discord":
            # ── Per-bot gateway registration ──────────────────────────
            logger.info("lifespan: entering discord per-bot gateway registration")
            try:
                channel_rows = await pool.fetch(
                    "SELECT bot_id, address FROM channels WHERE transport = 'discord'"
                )
                logger.info(
                    "lifespan: channels query returned %d row(s): %s",
                    len(channel_rows),
                    [f"{r['bot_id']}@{r['address']}" for r in channel_rows],
                )
            except Exception as _e:
                if _e.__class__.__name__ == "UndefinedTableError":
                    logger.warning(
                        "channels table not found (missing migration 0020?), "
                        "falling back to legacy path"
                    )
                    channel_rows = []
                else:
                    raise

            per_bot_tokens = settings.discord_bot_tokens
            legacy_token = settings.discord_bot_token
            logger.info(
                "lifespan: per-bot tokens available for bot_ids=%s, legacy_token=%s",
                sorted(per_bot_tokens.keys()),
                "set" if legacy_token else "absent",
            )

            # Determine which bots to start
            bot_entries: list[tuple[str, str]] = []  # (bot_id, token_value)

            for row in channel_rows:
                bot_id: str = row["bot_id"]
                if bot_id in per_bot_tokens:
                    token_val = per_bot_tokens[bot_id].get_secret_value()
                elif len(channel_rows) == 1 and legacy_token:
                    logger.warning(
                        "using legacy DISCORD_BOT_TOKEN for bot_id=%s (deprecated)",
                        bot_id,
                    )
                    token_val = legacy_token.get_secret_value()
                else:
                    logger.warning(
                        "no token configured for discord bot_id=%s, skipping",
                        bot_id,
                    )
                    continue
                bot_entries.append((bot_id, token_val))

            if not bot_entries and not channel_rows and legacy_token:
                logger.info(
                    "no channels rows found; synthesizing in-memory "
                    "mediator entry from legacy DISCORD_BOT_TOKEN"
                )
                bot_entries.append(("mediator", legacy_token.get_secret_value()))

            logger.info(
                "lifespan: will start gateways for bot_ids=%s",
                [bid for bid, _ in bot_entries],
            )
            app.state.discord_gateways: dict[str, discord.DiscordGatewayBot] = {}
            for bot_id, token_val in bot_entries:
                logger.info("lifespan: constructing DiscordClient for bot_id=%s (token_len=%d)", bot_id, len(token_val))
                client = discord.DiscordClient(bot_id, token_val)
                discord.register_client(bot_id, client)

                # Per-bot pacer (create on demand for non-mediator bots)
                pacer = app.state.discord_pacers.get(bot_id)
                if (
                    pacer is None
                    and _discord_provider_enabled(settings)
                    and settings.discord_pacing_enabled
                ):
                    pacer = DiscordPacer(
                        pool,
                        settings=settings,
                        send_typing=_make_send_typing(bot_id),
                    )
                    app.state.discord_pacers[bot_id] = pacer

                gateway = discord.DiscordGatewayBot(
                    bot_id,
                    client,
                    pool,
                    app.state.coalescer,
                    pacer=pacer,
                )
                app.state.discord_gateways[bot_id] = gateway

                await discord.catch_up_recent_messages(
                    pool, app.state.coalescer, client=client
                )

                if bot_id == "mediator":
                    await discord.seed_partner_users(pool)

                task = asyncio.create_task(gateway.run_forever())
                app.state.background_tasks.add(task)
                logger.info("started discord gateway for bot_id=%s", bot_id)

            # ── Summary diagnostics after registration ──────────────────
            started_ids = sorted(bid for bid, _ in bot_entries)
            skipped_ids = sorted(
                row["bot_id"]
                for row in channel_rows
                if row["bot_id"] not in {bid for bid, _ in bot_entries}
            )
            if started_ids:
                logger.info(
                    "startup diagnostic discord_gateways_started=%s",
                    ",".join(started_ids),
                )
            if skipped_ids:
                logger.info(
                    "startup diagnostic discord_gateways_skipped_no_token=%s",
                    ",".join(skipped_ids),
                )
            if not started_ids and not skipped_ids:
                logger.info("startup diagnostic discord_gateways=none_started")
        if settings.scheduler_enabled:
            await seed_heartbeat(pool, settings=settings)
            await seed_weekly_reflections(pool)
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
            gateways: dict = getattr(app.state, "discord_gateways", {})
            for gateway in gateways.values():
                await gateway.close()
            await whatsapp.close_client()
            await discord.close_all_clients()
            hooks.set_pool(None)


app = FastAPI(lifespan=lifespan)
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(whatsapp_router.router)
