"""Registry of bot profiles available to the shared runner."""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any
from uuid import UUID

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.ids import HABITS_BOT_ID, HECTOR_BOT_ID, MEDIATOR_BOT_ID, TANTE_ROSI_BOT_ID
from app.bots.mediator import MEDIATOR_BOT

logger = logging.getLogger(__name__)

_RELATIONSHIP_TOPIC_ID: UUID | None = None
_PREGNANCY_TOPIC_ID: UUID | None = None
_HABITS_TOPIC_ID: UUID | None = None

BOT_SPECS: dict[str, BotSpec] = {
    MEDIATOR_BOT.bot_id: MEDIATOR_BOT,
}

_STAGING_BOTS_REGISTERED = False


def _maybe_register_staging_bots() -> None:
    """Register staging-only bots (coach) lazily.

    Called from get_bot_spec on first access. Lazy registration avoids a
    circular import: coach.build_coach_spec() pulls TOOL_DISPATCH which
    transitively imports messaging/hooks/app.bots.registry — so doing it at
    module-import time deadlocks under STAGING=1.
    """
    global _STAGING_BOTS_REGISTERED
    if _STAGING_BOTS_REGISTERED:
        return
    _STAGING_BOTS_REGISTERED = True
    if os.environ.get("STAGING", "").lower() in {"1", "true", "yes"}:
        from app.bots.coach import build_coach_spec

        coach = build_coach_spec()
        BOT_SPECS[coach.bot_id] = coach

        from app.bots.tante_rosi import build_tante_rosi_spec

        rosi = build_tante_rosi_spec()
        BOT_SPECS[rosi.bot_id] = rosi

        from app.bots.hector import build_hector_spec

        hector = build_hector_spec()
        BOT_SPECS[hector.bot_id] = hector

        from app.bots.habits import build_habits_spec

        habits = build_habits_spec()
        BOT_SPECS[habits.bot_id] = habits

        # Patch mediator's tool_allowlist now that TOOL_DISPATCH is available.
        # MEDIATOR_BOT was constructed at module level before the tools module
        # was fully initialized (circular import), so its tool_allowlist
        # defaults to None.  Rebuild the BOT_SPECS entry with the correct
        # allowlist that excludes Hector-only tools.
        from app.services.tools.registry import HECTOR_ONLY_TOOLS, TOOL_DISPATCH

        _PREGNANCY_ONLY_TOOLS = frozenset({
            "set_pregnancy_edd", "correct_pregnancy_edd", "end_pregnancy",
        })
        mediator_allowlist = (
            frozenset(TOOL_DISPATCH.keys())
            - HECTOR_ONLY_TOOLS
            - _PREGNANCY_ONLY_TOOLS
        )
        # dataclasses.replace propagates ALL fields (including
        # provider_chain) from the source MEDIATOR_BOT, so future BotSpec
        # additions don't need a parallel listing here.
        BOT_SPECS[MEDIATOR_BOT_ID] = dataclasses.replace(
            MEDIATOR_BOT,
            tool_allowlist=mediator_allowlist,
        )


class UnknownBotSpec(ValueError):
    pass


def get_bot_spec(bot_id: str) -> BotSpec:
    _maybe_register_staging_bots()
    try:
        return BOT_SPECS[bot_id]
    except KeyError as exc:
        known = ", ".join(sorted(BOT_SPECS))
        raise UnknownBotSpec(f"unknown bot spec: {bot_id}; known specs: {known}") from exc


async def populate_mediator_spec_from_db(pool: Any) -> None:
    """Read mediator display_name from the bots table and rebuild MEDIATOR_BOT.

    All scope/topic/version fields are hardcoded mediator defaults.
    Only display_name comes from the database — the bots table is intentionally
    thin per §3. On miss, warns and keeps code defaults unchanged.
    """
    try:
        row = await pool.fetchrow(
            "SELECT display_name FROM bots WHERE id = $1",
            MEDIATOR_BOT_ID,
        )
    except Exception:
        logger.warning(
            "populate_mediator_spec_from_db: could not query bots table — "
            "keeping module-level defaults",
            exc_info=True,
        )
        return
    if row is None:
        logger.warning(
            "populate_mediator_spec_from_db: no mediator row in bots table — "
            "keeping module-level defaults"
        )
        return

    display_name = row["display_name"]
    # Reconstruct mediator spec via dataclasses.replace so provider_chain and
    # any future BotSpec fields are propagated automatically.
    from app.bots.mediator import MEDIATOR_STEP_INSTRUCTIONS

    from app.services.tools.registry import HECTOR_ONLY_TOOLS, TOOL_DISPATCH

    _PREGNANCY_ONLY_TOOLS = frozenset({
        "set_pregnancy_edd", "correct_pregnancy_edd", "end_pregnancy",
    })

    # Use dataclasses.replace so every field on MEDIATOR_BOT — including
    # provider_chain and any future BotSpec additions — survives the rebuild
    # without requiring a parallel field list here.
    rebuilt = dataclasses.replace(
        MEDIATOR_BOT,
        step_instructions=MEDIATOR_STEP_INSTRUCTIONS,
        display_name=display_name,
        primary_topic_slug="relationship",
        participants_shape="dyad",
        read_scopes=ReadScopes(
            topics=frozenset({"own"}),
            allow_cross_topic_peek=True,
            allow_cross_topic_status_injection=True,
        ),
        write_scopes=WriteScopes(
            topics=frozenset({"relationship"}),
            require_reason_for_cross_topic=True,
        ),
        cross_topic_policy="peek",
        tool_allowlist=(
            frozenset(TOOL_DISPATCH.keys()) - HECTOR_ONLY_TOOLS - _PREGNANCY_ONLY_TOOLS
        ),
    )
    BOT_SPECS[MEDIATOR_BOT_ID] = rebuilt
    logger.info(
        "populate_mediator_spec_from_db: mediator spec updated (display_name=%s)",
        display_name,
    )


async def populate_tante_rosi_spec_from_db(pool: Any) -> None:
    """Check for a Tante Rosi row in the bots table and register if present.

    Mirrors populate_mediator_spec_from_db but for Tante Rosi.  Row existence
    is the enablement gate — the bots table has no ``enabled`` column (only
    id, display_name, created_at per migration 0020).

    Phase 1 wires the gate; the prod bots row is inserted in Phase 2 (U3).
    Process restart is required after row insertion because registration runs
    once at startup.
    """
    try:
        row = await pool.fetchrow("SELECT 1 FROM bots WHERE id = $1", TANTE_ROSI_BOT_ID)
    except Exception:
        logger.warning(
            "populate_tante_rosi_spec_from_db: could not query bots table — "
            "keeping staging-only registration",
            exc_info=True,
        )
        return
    if row is None:
        logger.debug(
            "populate_tante_rosi_spec_from_db: no tante_rosi row in bots table — "
            "bot available only via STAGING=1"
        )
        return

    from app.bots.tante_rosi import build_tante_rosi_spec

    spec = build_tante_rosi_spec()
    BOT_SPECS[spec.bot_id] = spec
    logger.info(
        "populate_tante_rosi_spec_from_db: tante_rosi spec registered from bots table"
    )


async def populate_hector_spec_from_db(pool: Any) -> None:
    """Check for a Hector row in the bots table and register if present.

    Mirrors populate_tante_rosi_spec_from_db but for Hector.  Row existence
    is the enablement gate — the bots table has no ``enabled`` column (only
    id, display_name, created_at per migration 0020).

    Prod registration: the prod bots row is inserted by migration 0038.
    Restart is required after row insertion because registration runs
    once at startup.
    """
    try:
        row = await pool.fetchrow("SELECT 1 FROM bots WHERE id = $1", HECTOR_BOT_ID)
    except Exception:
        logger.warning(
            "populate_hector_spec_from_db: could not query bots table — "
            "keeping staging-only registration",
            exc_info=True,
        )
        return
    if row is None:
        logger.debug(
            "populate_hector_spec_from_db: no hector row in bots table — "
            "bot available only via STAGING=1"
        )
        return

    from app.bots.hector import build_hector_spec

    spec = build_hector_spec()
    BOT_SPECS[spec.bot_id] = spec
    logger.info(
        "populate_hector_spec_from_db: hector spec registered from bots table"
    )


async def populate_habits_spec_from_db(pool: Any) -> None:
    """Check for a Habits row in the bots table and register if present.

    Mirrors populate_hector_spec_from_db. Row existence is the enablement
    gate. The prod bots row is inserted by migration 0050. Restart is
    required after row insertion because registration runs once at startup.
    """
    try:
        row = await pool.fetchrow("SELECT 1 FROM bots WHERE id = $1", HABITS_BOT_ID)
    except Exception:
        logger.warning(
            "populate_habits_spec_from_db: could not query bots table — "
            "keeping staging-only registration",
            exc_info=True,
        )
        return
    if row is None:
        logger.debug(
            "populate_habits_spec_from_db: no habits row in bots table — "
            "bot available only via STAGING=1"
        )
        return

    from app.bots.habits import build_habits_spec

    spec = build_habits_spec()
    BOT_SPECS[spec.bot_id] = spec
    logger.info(
        "populate_habits_spec_from_db: habits spec registered from bots table"
    )


async def populate_topic_ids_from_db(pool: Any) -> None:
    """Read core topic ids from mediator.topics and cache them.

    Must be called at startup AFTER the pool is available. On miss the
    module-level slot stays None and a warning is logged — callers must
    tolerate get_relationship_topic_id() / get_pregnancy_topic_id() /
    get_habits_topic_id() returning None.
    """
    global _RELATIONSHIP_TOPIC_ID, _PREGNANCY_TOPIC_ID, _HABITS_TOPIC_ID
    try:
        rows = await pool.fetch(
            "SELECT id, slug FROM mediator.topics "
            "WHERE slug IN ('relationship', 'pregnancy', 'fitness', 'habits')"
        )
    except Exception:
        logger.warning(
            "populate_topic_ids_from_db: could not query topics table — "
            "topic ids unavailable",
            exc_info=True,
        )
        return
    for row in rows:
        slug = row["slug"]
        if slug == "relationship":
            _RELATIONSHIP_TOPIC_ID = row["id"]
            logger.info("populate_topic_ids_from_db: relationship topic id cached (%s)", _RELATIONSHIP_TOPIC_ID)
        elif slug == "pregnancy":
            _PREGNANCY_TOPIC_ID = row["id"]
            logger.info("populate_topic_ids_from_db: pregnancy topic id cached (%s)", _PREGNANCY_TOPIC_ID)
        elif slug == "habits":
            _HABITS_TOPIC_ID = row["id"]
            logger.info("populate_topic_ids_from_db: habits topic id cached (%s)", _HABITS_TOPIC_ID)
    if _RELATIONSHIP_TOPIC_ID is None:
        logger.warning(
            "populate_topic_ids_from_db: no relationship row in topics table — "
            "relationship topic id unavailable"
        )
    if _PREGNANCY_TOPIC_ID is None:
        logger.warning(
            "populate_topic_ids_from_db: no pregnancy row in topics table — "
            "pregnancy topic id unavailable (run migration 0033?)"
        )
    if _HABITS_TOPIC_ID is None:
        logger.warning(
            "populate_topic_ids_from_db: no habits row in topics table — "
            "habits topic id unavailable (run migration 0050?)"
        )


def get_relationship_topic_id() -> UUID | None:
    """Return the cached relationship topic id, or None if not yet populated."""
    return _RELATIONSHIP_TOPIC_ID


def get_pregnancy_topic_id() -> UUID | None:
    """Return the cached pregnancy topic id, or None if not yet populated."""
    return _PREGNANCY_TOPIC_ID


def get_habits_topic_id() -> UUID | None:
    """Return the cached habits topic id, or None if not yet populated."""
    return _HABITS_TOPIC_ID


_TOPIC_SLUG_CACHE: dict[str, UUID] = {}


async def primary_topic_id_for(pool: Any, bot_spec: BotSpec) -> UUID:
    """Resolve a bot's primary_topic_slug to a topic UUID.

    Uses a module-level cache (slug -> UUID) to avoid repeated DB lookups.
    For mediator (slug='relationship'), delegates to get_relationship_topic_id()
    for stability. Otherwise queries mediator.topics by slug.

    No hash() per lesson #4; schema 'mediator' per lesson #5.
    """
    slug = bot_spec.primary_topic_slug
    if slug == "relationship":
        cached = get_relationship_topic_id()
        if cached is not None:
            return cached
        # Fall through to DB query if relationship topic not yet cached

    if slug in _TOPIC_SLUG_CACHE:
        return _TOPIC_SLUG_CACHE[slug]

    row = await pool.fetchrow(
        "SELECT id FROM mediator.topics WHERE slug = $1",
        slug,
    )
    if row is None:
        raise ValueError(
            f"primary_topic_id_for: no topic found for slug={slug!r} "
            f"(bot_id={bot_spec.bot_id})"
        )
    topic_id: UUID = row["id"]
    _TOPIC_SLUG_CACHE[slug] = topic_id
    return topic_id
