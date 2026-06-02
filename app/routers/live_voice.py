"""Live voice agent router (Sprint-0 stubs).

Endpoints under ``/api/live`` plus a stub WebSocket at ``/ws/live/{session_id}``
power the React UI in ``web/live-voice``.  This sprint wires the surface area
so the front-end can talk to a real backend; later sprints will swap the
WebSocket stub for actual realtime audio + Haiku turn handling.

TODOs intentionally left in place:
- Persona scoping (currently returns *all* registered bots; later: scope to
  the caller's ``mediator.bot_bindings`` rows).
- Auth (currently uses ``LIVE_VOICE_TEST_USER_ID`` from settings as a
  placeholder caller id).
- WebSocket handler (currently echo + a single phase message).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.bots.registry import BOT_SPECS, _maybe_register_staging_bots, primary_topic_id_for
from app.config import get_settings
from app.db import get_pool
from app.services.auth import jwt as live_jwt
from app.services.live.prep import (
    produce_agenda,
    retry_live_prep,
    run_live_prep_agentic_job,
    select_agenda_producer,
)
from app.services.live.debrief import (
    retry_live_debrief,
    run_live_debrief_agentic_job,
)
from app.services.live.rate_limit import WS_RATE_LIMITER
from app.services.live.schemas import PrepRequest, TurnRequest
from app.services.live.status import canonicalize_status, normalize_row_status
from app.services.live.stt import select_transcriber
from app.services.charge import classify_charge
from app.services.message_embedding_lifecycle import (
    enqueue_conversation_note_embed,
)
from app.services.live.budget import (
    HARD_CAP_CENTS,
    SOFT_CAP_CENTS,
    charge_session,
    check_budget,
)
from app.services.live.synthesis import finalize_session, save_review, synthesize_review
from app.services.live.turn_loop import (
    apply_emission,
    fallback_turn_emission,
    load_turn_context,
    select_turn_caller,
)
from app.services.live.tts import select_tts_provider

# --------------------------------------------------------------------------- #
# In-memory event-rate counters for the alarms endpoint.
# Simple sliding window of (timestamp, event_kind) over the last 10 min.
# Resets on process restart — fine for the briefing's single-replica deploy;
# would be Redis-backed in a multi-replica setup.
# --------------------------------------------------------------------------- #

from collections import deque as _deque
from time import time as _time

_LIVE_EVENTS: _deque = _deque(maxlen=4096)


def _record_event(kind: str) -> None:
    _LIVE_EVENTS.append((_time(), kind))


def _event_rate_5m(numerator_kind: str, denominator_kind: str) -> float:
    cutoff = _time() - 300.0
    num = den = 0
    for ts, k in _LIVE_EVENTS:
        if ts < cutoff:
            continue
        if k == denominator_kind:
            den += 1
        if k == numerator_kind:
            num += 1
    return num / den if den > 0 else 0.0


_CRISIS_UTTERANCE = (
    "I'm staying with you. Right now, what matters most is reaching someone who "
    "can be present with you safely. If you're in the US or Canada, please call or "
    "text 988. UK / Ireland: Samaritans at 116 123. Australia: Lifeline 13 11 14. "
    "If you're somewhere else, please reach a local crisis line or emergency services. "
    "I'm here while you do that."
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Models ───────────────────────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    model_config = {"extra": "ignore"}

    bot_id: str = Field(..., description="Persona id, e.g. 'tante_rosi'")
    topic: str | None = Field(default=None, description="Optional topic label / slug")
    steering_text: str | None = Field(
        default=None,
        description="Optional steering text; presence flips mode to 'steered'",
    )
    skip_prep: bool = Field(
        default=False,
        description="When true, skip live-prep agenda generation and open the mic directly.",
    )


class CreateSessionResponse(BaseModel):
    session_id: UUID
    mode: str
    status: str
    prep_pending: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────


# Stable placeholder so dev runs without auth still produce a valid UUID
# in mediator.conversations.user_id (which is presumed UUID-typed).
_DEFAULT_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _resolve_test_user_id() -> UUID:
    """Read LIVE_VOICE_TEST_USER_ID for unauthenticated local live-voice runs."""
    raw = (
        os.environ.get("LIVE_VOICE_TEST_USER_ID")
        or getattr(get_settings(), "live_voice_test_user_id", "")
        or ""
    ).strip()
    if not raw:
        return _DEFAULT_TEST_USER_ID
    try:
        return UUID(raw)
    except ValueError:
        logger.warning(
            "live_voice: LIVE_VOICE_TEST_USER_ID=%r is not a valid UUID; "
            "falling back to placeholder",
            raw,
        )
        return _DEFAULT_TEST_USER_ID


def get_current_user(request: Request) -> UUID:
    """Return the caller's UUID from the Bearer JWT, or the dev placeholder.

    When ``live_voice_auth_enabled`` is False (default) the function bypasses
    token verification and returns the configured test user id.  When enabled,
    the ``Authorization: Bearer <token>`` header is required; a missing header,
    invalid signature, or non-UUID ``user_id`` claim all raise 401.
    """
    if not get_settings().live_voice_auth_enabled:
        return _resolve_test_user_id()
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = auth_header[len("Bearer "):]
    try:
        claims = live_jwt.verify(token)
        return UUID(claims.user_id)
    except (live_jwt.JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def _require_ownership(pool: Any, session_id: UUID, user_id: UUID) -> dict:
    """Fetch a conversation row and verify that ``user_id`` is an owner.

    Raises 404 if the row does not exist, 403 if the caller is neither the
    primary user nor the partner.  Returns ``dict(row)`` on success.

    Uses a targeted column SELECT (never SELECT *) and guards against NULL
    ``partner_user_id`` before comparison.
    """
    row = await pool.fetchrow(
        "SELECT id, user_id, partner_user_id, status FROM mediator.conversations WHERE id=$1",
        session_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    row_dict = dict(row)
    is_owner = row_dict["user_id"] == user_id or (
        row_dict["partner_user_id"] is not None and row_dict["partner_user_id"] == user_id
    )
    if not is_owner:
        raise HTTPException(status_code=403, detail="Not an owner of this session")
    return row_dict


def _require_operator(user_id: UUID) -> None:
    """Authorize an operator-only (``/api/live/ops/*``) request.

    These endpoints leak aggregate ops data and full session transcripts, so
    they are gated behind an explicit operator allow-list
    (``live_voice_ops_user_ids``) on top of ``get_current_user``.

    Behaviour mirrors the ownership gate's flag-aware pattern: when
    ``live_voice_auth_enabled`` is False (local dev / tests) the check is a
    no-op so the debug tooling keeps working.  When auth is enabled, the
    caller's id MUST appear in the configured allow-list, otherwise 403.  An
    empty allow-list therefore fails closed (no operator ⇒ nobody allowed).
    """
    if not get_settings().live_voice_auth_enabled:
        return
    allowed = get_settings().live_voice_ops_user_id_set
    if str(user_id) not in allowed:
        raise HTTPException(status_code=403, detail="Operator access required")


async def _conversations_table_exists(pool: Any) -> bool:
    """Check whether ``mediator.conversations`` is present.

    Used by /healthz and /sessions to give a clean error before the migration
    lands.
    """
    try:
        present = await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'mediator'
                  AND table_name = 'conversations'
            )
            """,
        )
    except Exception:
        logger.warning("live_voice: failed to probe mediator.conversations", exc_info=True)
        return False
    return bool(present)


# ── REST endpoints ───────────────────────────────────────────────────────────


@router.get("/api/live/healthz")
async def healthz(pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Liveness + dependency snapshot for the live-voice surface."""
    checks: dict[str, Any] = {}

    # DB reachable?
    try:
        await pool.fetchval("SELECT 1")
        checks["db"] = {"ok": True}
    except Exception as exc:
        checks["db"] = {"ok": False, "error": str(exc)}

    # mediator.conversations present?
    has_conversations = await _conversations_table_exists(pool)
    checks["conversations_table"] = {
        "ok": has_conversations,
        "detail": (
            "mediator.conversations present"
            if has_conversations
            else "mediator.conversations missing — run live-voice migration"
        ),
    }

    # OPENAI_API_KEY available?
    settings = get_settings()
    openai_key_present = bool(
        settings.openai_api_key and settings.openai_api_key.get_secret_value()
    )
    checks["openai_api_key"] = {"ok": openai_key_present}

    overall_ok = checks["db"]["ok"] and openai_key_present
    # NB: missing conversations table is *not* a hard fail per spec.
    return {"ok": overall_ok, "checks": checks}


async def _bound_bot_ids(pool: Any, user_id: UUID) -> set[str]:
    """Return the bot_ids that this user is bound to.

    Mirrors the pattern in ``app/services/routing.py::resolve_binding`` —
    a row matches if it's a direct user binding OR if the user is a member
    of the bound dyad. Returns the empty set when no rows match (the caller
    decides what fallback to use).
    """
    try:
        rows = await pool.fetch(
            """
            SELECT DISTINCT bb.bot_id
            FROM bot_bindings bb
            LEFT JOIN dyad_members dm ON dm.dyad_id = bb.dyad_id
            WHERE bb.user_id = $1 OR dm.user_id = $1
            """,
            user_id,
        )
    except Exception:
        logger.warning("live_voice: bot_bindings lookup failed", exc_info=True)
        return set()
    return {row["bot_id"] for row in rows}


async def _resolve_live_partner_user_id(
    pool: Any, user_id: UUID, bot_id: str
) -> UUID | None:
    """Find the other dyad member for this user's bot binding, if any."""
    try:
        row = await pool.fetchrow(
            """
            SELECT other_dm.user_id
            FROM bot_bindings bb
            JOIN dyad_members self_dm
              ON self_dm.dyad_id = bb.dyad_id
             AND self_dm.user_id = $1
            JOIN dyad_members other_dm
              ON other_dm.dyad_id = bb.dyad_id
             AND other_dm.user_id <> $1
            WHERE bb.bot_id = $2
            ORDER BY other_dm.user_id
            LIMIT 1
            """,
            user_id,
            bot_id,
        )
    except Exception:
        logger.warning("live_voice: partner lookup failed", exc_info=True)
        return None
    if row is None:
        return None
    try:
        return UUID(str(row["user_id"]))
    except (TypeError, ValueError):
        return None


@router.get("/api/live/personas")
async def list_personas(
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Return personas the caller may steer.

    Scoped to the caller's rows in ``bot_bindings`` (joined through
    ``dyad_members`` when the binding is dyadic). When no bindings match
    (fresh install / unauthed dev), returns the full ``BOT_SPECS`` registry
    with ``scoped=false`` so the frontend can surface a "dev mode" hint.
    """
    _maybe_register_staging_bots()

    bound = await _bound_bot_ids(pool, user_id)
    if bound:
        specs = [s for s in BOT_SPECS.values() if s.bot_id in bound]
        scoped = True
    else:
        specs = list(BOT_SPECS.values())
        scoped = False

    personas = [
        {
            "bot_id": spec.bot_id,
            "display_name": spec.display_name,
            "topic": spec.primary_topic_slug,
        }
        for spec in sorted(specs, key=lambda s: s.display_name.lower())
    ]
    return {"personas": personas, "scoped": scoped, "user_id": str(user_id)}


@router.get("/api/live/config")
async def public_config() -> dict[str, Any]:
    """Public client config — used by the React app to render conditional UI."""
    settings = get_settings()
    openai_key_present = bool(
        settings.openai_api_key and settings.openai_api_key.get_secret_value()
    )
    return {
        "discord_oauth_enabled": False,  # OAuth deferred to v1.1 — see R5.
        "auth_mode": "magic_link",       # v1 auth path: Discord DM magic-link.
        "magic_link_enabled": True,
        "openai_voice_enabled": openai_key_present,
        "env_name": settings.env_name,
    }


@router.get("/api/live/sessions")
async def list_sessions(
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
    status: str | None = Query(None),
) -> dict[str, Any]:
    """Return sessions where the caller is owner or partner, newest first."""
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")

    if status is not None:
        rows = await pool.fetch(
            """
            SELECT id, status, bot_id, prep_summary, steering_text, created_at,
                   (SELECT COUNT(*) FROM mediator.conversation_items ci
                    WHERE ci.conversation_id = c.id) AS item_count
            FROM mediator.conversations c
            WHERE (user_id = $1 OR partner_user_id = $1)
              AND status = $2
            ORDER BY created_at DESC
            """,
            user_id,
            status,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, status, bot_id, prep_summary, steering_text, created_at,
                   (SELECT COUNT(*) FROM mediator.conversation_items ci
                    WHERE ci.conversation_id = c.id) AS item_count
            FROM mediator.conversations c
            WHERE (user_id = $1 OR partner_user_id = $1)
            ORDER BY created_at DESC
            """,
            user_id,
        )

    sessions = []
    for row in rows:
        bot_spec = BOT_SPECS.get(row["bot_id"])
        topic_label = bot_spec.display_name if bot_spec else row["bot_id"]
        sessions.append({
            "id": str(row["id"]),
            "status": canonicalize_status(row["status"]),
            "bot_id": row["bot_id"],
            "topic_label": topic_label,
            "prep_summary": row["prep_summary"],
            "steering_text": row["steering_text"],
            "item_count": int(row["item_count"]),
            "created_at": row["created_at"],
        })

    return {"sessions": sessions}


@router.post("/api/live/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> CreateSessionResponse:
    """Create a new live-voice conversation + run prep (Sprint 2).

    Inserts the ``mediator.conversations`` row in ``prepping`` status
    up-front and returns immediately.  When ``LIVE_VOICE_PREP_PROVIDER=stub``
    the legacy synchronous path is used instead (no async prep).

    Agentic prep (default) schedules ``run_live_prep_agentic_job`` via
    ``asyncio.create_task``.  The client polls ``/card`` to observe
    ``prepping`` → ``ready`` or ``prep_failed``.
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(
            status_code=503,
            detail="live conversations not yet migrated",
        )

    # Validate bot_id against the registry (fail fast with 400 instead of FK
    # violation surfacing as a 500).
    _maybe_register_staging_bots()
    if body.bot_id not in BOT_SPECS:
        known = ", ".join(sorted(BOT_SPECS))
        raise HTTPException(
            status_code=400,
            detail=f"unknown bot_id={body.bot_id!r}; known: {known}",
        )

    bot_spec = BOT_SPECS[body.bot_id]
    session_id = uuid4()
    mode = "steered" if (body.steering_text or "").strip() else "open"
    steering = body.steering_text
    partner_user_id = await _resolve_live_partner_user_id(pool, user_id, body.bot_id)

    # Resolve topic_id from the bot's primary_topic_slug.
    topic_id: UUID | None = None
    try:
        topic_id = await primary_topic_id_for(pool, bot_spec)
    except Exception:
        logger.warning(
            "live_voice: could not resolve primary topic for bot_id=%s, "
            "leaving topic_id=NULL",
            body.bot_id,
        )

    # Insert the conversations row in 'prepping' status up-front.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO mediator.conversations
                    (id, user_id, partner_user_id, bot_id, mode, steering_text, status,
                     prep_summary, current_item_id, topic_id)
                VALUES ($1, $2, $3, $4, $5, $6, 'preparing', NULL, NULL, $7)
                """,
                session_id,
                user_id,
                partner_user_id,
                body.bot_id,
                mode,
                steering,
                topic_id,
            )

    # Determine the prep path.
    if body.skip_prep:
        await pool.execute(
            """
            UPDATE mediator.conversations
            SET status = 'ready',
                prep_summary = $2
            WHERE id = $1::uuid
            """,
            session_id,
            "Just speak mode: no prep brief was generated.",
        )
        return CreateSessionResponse(
            session_id=session_id,
            mode=mode,
            status="ready",
            prep_pending=False,
        )

    producer = select_agenda_producer()

    if producer is not None:
        # Legacy synchronous path (stub, anthropic, or deepseek override).
        request = PrepRequest(
            user_id=str(user_id),
            bot_id=body.bot_id,
            steering_text=steering,
            topic_slug=body.topic,
        )
        try:
            result = await produce_agenda(pool, request, producer=producer, session_id=session_id)
        except Exception as exc:
            logger.exception("live_voice: legacy prep failed")
            # Mark the session as prep_failed.
            try:
                from app.services.live.prep import _set_prep_failed
                await _set_prep_failed(pool, session_id, str(exc))
            except Exception:
                logger.warning("live_voice: failed to mark prep_failed", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"failed to prep live session: {exc}",
            ) from exc
        return CreateSessionResponse(
            session_id=UUID(result.session_id),
            mode=mode,
            status="ready",
            prep_pending=False,
        )

    # Agentic async path (default) — schedule background prep.
    async def _background_prep() -> None:
        try:
            await run_live_prep_agentic_job(
                conversation_id=session_id,
                user_id=user_id,
                bot_id=body.bot_id,
                steering_text=steering,
                topic_id=topic_id,
                pool=pool,
            )
        except Exception:
            logger.exception(
                "live_voice: background prep crashed for session_id=%s",
                session_id,
            )
            try:
                from app.services.live.prep import _set_prep_failed
                await _set_prep_failed(
                    pool, session_id, "background prep task crashed"
                )
            except Exception:
                logger.warning(
                    "live_voice: failed to mark prep_failed after crash",
                    exc_info=True,
                )

    asyncio.create_task(_background_prep())

    return CreateSessionResponse(
        session_id=session_id,
        mode=mode,
        status="preparing",
        prep_pending=True,
    )


@router.get("/api/live/sessions/{session_id}/card")
async def get_session_card(
    session_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the session card payload: prep_summary + items grouped by theme.

    The session card is what the user sees before pressing Start; the raw
    agenda is never exposed.  Handles ``prepping`` / ``prep_failed`` / ``ready``
    statuses (Sprint 2).
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)

    conv = await pool.fetchrow(
        """
        SELECT id, user_id, bot_id, mode, status, prep_summary,
               current_item_id, started_at, session_fields
        FROM mediator.conversations
        WHERE id = $1
        """,
        session_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="session not found")

    status = canonicalize_status(conv["status"])

    # Preparing (canonical) / prepping (legacy): return pending flag, empty items.
    if status == "preparing":
        return {
            "session_id": str(conv["id"]),
            "bot_id": conv["bot_id"],
            "mode": conv["mode"],
            "status": "preparing",
            "prep_pending": True,
            "prep_summary": None,
            "current_item_id": None,
            "items": [],
            "failure_reason": None,
        }

    # Prep failed: return failure reason from session_fields, empty items.
    if status == "prep_failed":
        failure_reason: str | None = None
        sf = conv["session_fields"] or {}
        if isinstance(sf, dict):
            failure_reason = sf.get("prep_error") or sf.get("prep_failure_reason")
        return {
            "session_id": str(conv["id"]),
            "bot_id": conv["bot_id"],
            "mode": conv["mode"],
            "status": "prep_failed",
            "prep_pending": False,
            "prep_summary": None,
            "current_item_id": None,
            "items": [],
            "failure_reason": failure_reason,
        }

    # Ready: existing behaviour (unchanged).
    items = await pool.fetch(
        """
        SELECT ci.id, ci.title, ci.intent, ci.ask, ci.done_when,
               ci.kind, ci.priority, ci.speaker_scope,
               ci.coverage_evidence_required, ci.order_hint,
               ci.theme_id, t.title AS theme_label
        FROM mediator.conversation_items ci
        LEFT JOIN mediator.themes t ON t.id = ci.theme_id
        WHERE ci.conversation_id = $1
        ORDER BY ci.order_hint, ci.created_at
        """,
        session_id,
    )

    return {
        "session_id": str(conv["id"]),
        "bot_id": conv["bot_id"],
        "mode": conv["mode"],
        "status": status,
        "prep_summary": conv["prep_summary"],
        "current_item_id": str(conv["current_item_id"]) if conv["current_item_id"] else None,
        "prep_pending": False,
        "items": [
            {
                "id": str(row["id"]),
                "title": row["title"],
                "intent": row["intent"],
                "ask": row["ask"],
                "done_when": row["done_when"],
                "kind": row["kind"],
                "priority": row["priority"],
                "speaker_scope": row["speaker_scope"],
                "coverage_evidence_required": row["coverage_evidence_required"],
                "theme": (
                    {"slug": str(row["theme_id"]), "label": row["theme_label"]}
                    if row["theme_id"]
                    else None
                ),
            }
            for row in items
        ],
        "failure_reason": None,
    }


@router.post("/api/live/sessions/{session_id}/prep/retry")
async def retry_prep(
    session_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Retry a failed live-prep session.

    Only accepts sessions in ``prep_failed`` status (409 otherwise).
    Resets status to ``prepping`` and schedules a new agentic prep task.
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)

    row = await pool.fetchrow(
        "SELECT id, status FROM mediator.conversations WHERE id = $1",
        session_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")

    if row["status"] != "prep_failed":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry prep for session in status={row['status']!r}; "
                f"only 'prep_failed' sessions are retryable"
            ),
        )

    # retry_live_prep owns the status transition; caller must NOT pre-set status.

    # Schedule the retry as a background task.
    async def _background_retry() -> None:
        try:
            await retry_live_prep(
                conversation_id=session_id,
                pool=pool,
            )
        except Exception:
            logger.exception(
                "live_voice: background retry prep crashed for session_id=%s",
                session_id,
            )
            try:
                from app.services.live.prep import _set_prep_failed
                await _set_prep_failed(
                    pool, session_id, "background retry task crashed"
                )
            except Exception:
                logger.warning(
                    "live_voice: failed to mark prep_failed after retry crash",
                    exc_info=True,
                )

    asyncio.create_task(_background_retry())

    return {
        "session_id": str(session_id),
        "status": "preparing",
        "prep_pending": True,
    }


@router.post("/api/live/sessions/{session_id}/debrief/retry")
async def retry_debrief(
    session_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Retry a failed live-debrief session.

    Only accepts sessions in ``debrief_failed`` status (409 otherwise).
    Schedules a new agentic debrief task. The retry helper validates the
    current ``debrief_failed`` state and performs the transition to
    ``debriefing`` itself.

    Gated behind ``live_debrief_agentic_enabled`` feature flag (403 when off).
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")

    settings = get_settings()
    if not settings.live_debrief_agentic_enabled:
        raise HTTPException(status_code=403, detail="live_debrief_agentic not enabled")
    if settings.live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)

    row = await pool.fetchrow(
        "SELECT id, status FROM mediator.conversations WHERE id = $1",
        session_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")

    if row["status"] != "debrief_failed":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry debrief for session in status={row['status']!r}; "
                f"only 'debrief_failed' sessions are retryable"
            ),
        )

    # Schedule the retry as a background task.
    async def _background_retry() -> None:
        try:
            await retry_live_debrief(
                conversation_id=session_id,
                pool=pool,
            )
        except Exception:
            logger.exception(
                "live_voice: background retry debrief crashed for session_id=%s",
                session_id,
            )
            try:
                from app.services.live.debrief import _set_debrief_failed
                await _set_debrief_failed(
                    pool, session_id,
                    "background retry task crashed",
                    turn_id=None,
                    tool_call_count=0,
                )
            except Exception:
                logger.warning(
                    "live_voice: failed to mark debrief_failed after retry crash",
                    exc_info=True,
                )

    asyncio.create_task(_background_retry())

    return {
        "session_id": str(session_id),
        "status": "debriefing",
        "debrief_pending": True,
    }


class ConsentBody(BaseModel):
    model_config = {"extra": "ignore"}
    kind: str = Field(..., description="'solo' or 'partner_present'")
    partner_label: str | None = Field(default=None, max_length=80)


@router.post("/api/live/sessions/{session_id}/consent")
async def post_consent(
    session_id: UUID,
    body: ConsentBody,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Record the pre-mic consent decision.

    Writes a conversation_consent_events row for the primary speaker
    (granted) and, when partner_present, a second row for the partner
    keyed on the partner_label. Both rows are atomic under one txn so
    "consent recorded" implies "ready to open the mic".
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)
    if body.kind not in ("solo", "partner_present"):
        raise HTTPException(status_code=400, detail="kind must be 'solo' or 'partner_present'")
    if body.kind == "partner_present" and not (body.partner_label or "").strip():
        raise HTTPException(status_code=400, detail="partner_label required when partner_present")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO mediator.conversation_consent_events
                    (conversation_id, speaker_label, role, event_type, method)
                VALUES ($1, 'speaker_0', 'primary', 'granted', 'screen_tap')
                """,
                session_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.conversation_speakers
                    (conversation_id, speaker_label, role, consent_state, consented_at)
                VALUES ($1, 'speaker_0', 'primary', 'granted', now())
                ON CONFLICT (conversation_id, speaker_label) DO UPDATE
                SET consent_state = 'granted', consented_at = now()
                """,
                session_id,
            )
            if body.kind == "partner_present":
                partner_label = body.partner_label.strip()
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_consent_events
                        (conversation_id, speaker_label, role, event_type, method, note)
                    VALUES ($1, $2, 'partner', 'granted', 'screen_tap', 'partner acknowledged by primary')
                    """,
                    session_id,
                    partner_label,
                )
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_speakers
                        (conversation_id, speaker_label, role, consent_state, consented_at)
                    VALUES ($1, $2, 'partner', 'granted', now())
                    ON CONFLICT (conversation_id, speaker_label) DO UPDATE
                    SET consent_state = 'granted', consented_at = now()
                    """,
                    session_id,
                    partner_label,
                )
    return {"ok": True, "kind": body.kind, "partner_label": body.partner_label}


@router.post("/api/live/sessions/{session_id}/end")
async def end_session(
    session_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Flip the session to review_pending (or debriefing) and synthesize review.

    When ``live_debrief_agentic_enabled`` is True, the session transitions to
    ``debriefing`` and a background debrief job is queued.  The response still
    includes the deterministic synthesis immediately.
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)

    new_status = await finalize_session(pool, session_id)
    review = await synthesize_review(pool, session_id)
    # Normalize the status in the review response to canonical form.
    review["status"] = canonicalize_status(review.get("status", ""))

    settings = get_settings()
    if settings.live_debrief_agentic_enabled and new_status == "debriefing":
        # Queue the agentic debrief as a background task.
        async def _background_debrief() -> None:
            try:
                # Load the user row so we can pass a proper User object.
                conv_row = await pool.fetchrow(
                    "SELECT user_id FROM mediator.conversations WHERE id = $1",
                    session_id,
                )
                if conv_row is None:
                    logger.error(
                        "live_voice: background debrief cannot find session_id=%s",
                        session_id,
                    )
                    return
                user_row = await pool.fetchrow(
                    "SELECT * FROM users WHERE id = $1", conv_row["user_id"]
                )
                if user_row is None:
                    logger.error(
                        "live_voice: background debrief cannot find user for session_id=%s",
                        session_id,
                    )
                    return
                from app.services.live.bot_profile import user_from_live_row
                user = user_from_live_row(conv_row["user_id"], user_row)

                await run_live_debrief_agentic_job(
                    conversation_id=session_id,
                    user=user,
                    pool=pool,
                )
            except Exception:
                logger.exception(
                    "live_voice: background debrief crashed for session_id=%s",
                    session_id,
                )
                try:
                    from app.services.live.debrief import _set_debrief_failed
                    await _set_debrief_failed(
                        pool, session_id,
                        "background debrief task crashed",
                        turn_id=None,
                        tool_call_count=0,
                    )
                except Exception:
                    logger.warning(
                        "live_voice: failed to mark debrief_failed after crash",
                        exc_info=True,
                    )

        asyncio.create_task(_background_debrief())
        review["debrief_pending"] = True

    return review


@router.get("/api/live/sessions/{session_id}/review")
async def get_review(
    session_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Return session review, preferring debrief artifact when available.

    Sprint 5 (T5): When a non-deleted ``live_debrief`` artifact exists (highest
    ``revision_number``), its payload is adapted via
    ``_debrief_artifact_to_session_review`` and becomes the primary response.
    Falls back to deterministic :func:`synthesis.synthesize_review` for old
    conversations or missing / invalid artifacts — never 500 or blank.

    Additive fields (``debrief_pending``, ``debrief_failed``,
    ``live_debrief``, ``review_summary``) remain backward-compatible.
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)

    # ── 1. Load the conversation row for status + metadata ───────────────
    conv = await pool.fetchrow(
        """
        SELECT id, status, session_fields
        FROM mediator.conversations
        WHERE id = $1
        """,
        session_id,
    )
    status = canonicalize_status(conv["status"]) if conv else ""

    # ── 2. Prefer highest-revision live_debrief artifact ─────────────────
    try:
        from app.services.live import artifacts as live_artifacts
        debrief_artifact = await live_artifacts.get_current_artifact(
            pool,
            conversation_id=str(session_id),
            artifact_type="live_debrief",
        )
    except Exception:
        logger.debug(
            "live_voice: artifact lookup failed for %s — falling back to synthesize_review",
            session_id,
            exc_info=True,
        )
        debrief_artifact = None

    if debrief_artifact is not None and isinstance(debrief_artifact.payload, dict):
        # ── Artifact present: adapt its payload into the SessionReview shape ─
        from app.services.live.adapters import _debrief_artifact_to_session_review

        try:
            review = _debrief_artifact_to_session_review(debrief_artifact.payload)
        except Exception:
            logger.warning(
                "live_voice: _debrief_artifact_to_session_review raised for %s "
                "— falling back to synthesize_review",
                session_id,
                exc_info=True,
            )
            review = await synthesize_review(pool, session_id)
            review["status"] = canonicalize_status(review.get("status", ""))
        else:
            # Ensure session_id is always present.
            if "session_id" not in review:
                review["session_id"] = str(session_id)
            # Normalize status: prefer the conversation row status (canonicalized).
            if conv is not None:
                review["status"] = status

        # ── Always include the raw artifact payload for backward compat ──
        review["live_debrief"] = debrief_artifact.payload

        # ── Also check for review_summary artifact ───────────────────────
        try:
            review_summary_artifact = await live_artifacts.get_current_artifact(
                pool,
                conversation_id=str(session_id),
                artifact_type="review_summary",
            )
            if (
                review_summary_artifact is not None
                and review_summary_artifact.payload
            ):
                review["review_summary"] = review_summary_artifact.payload.get(
                    "review_summary"
                )
        except Exception:
            logger.debug(
                "live_voice: review_summary artifact lookup failed for %s",
                session_id,
                exc_info=True,
            )
    else:
        # ── No artifact: fall back to deterministic synthesis ────────────
        review = await synthesize_review(pool, session_id)
        review["status"] = canonicalize_status(review.get("status", ""))

    # ── 3. Enrich with status-dependent additive fields ──────────────────
    if conv is None:
        return review

    if status == "debriefing":
        review["debrief_pending"] = True

    elif status == "debrief_failed":
        sf = conv["session_fields"] or {}
        if isinstance(sf, dict):
            review["debrief_failed"] = {
                "reason": sf.get("debrief_failure_reason"),
                "error": sf.get("debrief_error"),
                "failed_at": sf.get("debrief_failed_at"),
            }
        else:
            review["debrief_failed"] = {"reason": "unknown"}

    return review


class SaveReviewBody(BaseModel):
    model_config = {"extra": "ignore"}
    keep_items: list[dict[str, Any]] = Field(default_factory=list)
    keep_notes: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/api/live/sessions/{session_id}/review/save")
async def save_review_endpoint(
    session_id: UUID,
    body: SaveReviewBody,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)
    counts = await save_review(
        pool,
        session_id,
        keep_items=body.keep_items,
        keep_notes=body.keep_notes,
    )
    return {"ok": True, "status": "completed", "counts": counts}


@router.get("/api/live/ops/metrics")
async def ops_metrics(
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Operator-facing metrics snapshot the briefing's alarms can scrape.

    Returns:
      * ``latency`` — p50/p95/p99 per stage (last 5 min)
      * ``spend_usd_today`` — summed across all sessions started today
      * ``active_sessions`` — count of conversations in active / pending status
      * ``status_counts`` — all conversations grouped by canonical status
        (folds legacy values into canonical via ``grouped_status_metric``)
      * ``recent_status_counts`` — status counts for conversations created
        in the last 24 hours (canonical + legacy folded)
      * ``error_rate_5m`` — 5xx as fraction of WS bot_turn attempts
      * ``ws_disconnect_rate_5m`` — unexpected disconnects as fraction of opens
    """
    _require_operator(user_id)
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    latency_rows = await pool.fetch(
        """
        SELECT stage,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY elapsed_ms) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY elapsed_ms) AS p99,
               count(*) AS samples
        FROM mediator.live_session_latency
        WHERE created_at >= now() - interval '5 minutes'
        GROUP BY stage
        """,
    )
    latency = {
        r["stage"]: {
            "p50": int(r["p50"] or 0),
            "p95": int(r["p95"] or 0),
            "p99": int(r["p99"] or 0),
            "samples": int(r["samples"] or 0),
        }
        for r in latency_rows
    }
    spend_row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(spend_usd_cents), 0) AS total
        FROM mediator.conversations
        WHERE started_at::date = (now() at time zone 'utc')::date
        """,
    )
    # Active sessions (canonical + legacy).
    active_count = await pool.fetchval(
        """
        SELECT count(*) FROM mediator.conversations
        WHERE status IN ('preparing', 'ready', 'active', 'review_pending',
                         'prepping', 'live', 'synthesizing')
        """
    )
    # ── Global status counts (all time) ─────────────────────────────────
    status_rows = await pool.fetch(
        "SELECT status, count(*) AS cnt FROM mediator.conversations GROUP BY status"
    )
    from app.services.live.status import grouped_status_metric
    normalized_statuses = grouped_status_metric(
        [{"status": r["status"]} for r in status_rows]
    )

    # ── Recent status counts (last 24 hours) ────────────────────────────
    recent_rows = await pool.fetch(
        """
        SELECT status, count(*) AS cnt
        FROM mediator.conversations
        WHERE created_at >= now() - interval '24 hours'
        GROUP BY status
        """
    )
    recent_statuses = grouped_status_metric(
        [{"status": r["status"]} for r in recent_rows]
    )

    return {
        "latency_ms": latency,
        "spend_usd_today": (int(spend_row["total"]) if spend_row else 0) / 100.0,
        "active_sessions": int(active_count or 0),
        "status_counts": normalized_statuses,
        "recent_status_counts": recent_statuses,
        "error_rate_5m": _event_rate_5m("ws_5xx", "ws_open"),
        "ws_disconnect_rate_5m": _event_rate_5m("ws_unexpected_disconnect", "ws_open"),
        "thresholds": {
            "p95_ear_to_ear_ms": 2000,
            "error_rate_5m": 0.01,
            "ws_disconnect_rate_5m": 0.05,
        },
    }


# ── Operator debug endpoint (internal, no auth changes) ──────────────────────


def _classify_failure(failure_reason: str | None) -> str:
    """Classify a failure_reason string into a durable failure class.

    Uses the same mapping as ``app/services/inbound_queue.FAILURE_REASON_TO_CLASS``
    but operates independently so the debug endpoint does not depend on the
    inbound-queue module.  Unknown / None reasons map to ``"infra_bug"``.
    """
    if not failure_reason:
        return "infra_bug"
    _MAP: dict[str, str] = {
        "provider_send_failed": "retryable_pre_send",
        "llm_timeout": "retryable_pre_send",
        "llm_phase_failed": "retryable_pre_send",
        "tool_validation_recoverable_exhausted": "retryable_pre_send",
        "crashed": "infra_bug",
        "transcription_failed": "infra_bug",
        "vision_failed": "infra_bug",
        "live_prep_submit_missing": "infra_bug",
        "live_prep_text_no_submit": "infra_bug",
        "live_prep_no_model_output": "infra_bug",
        "bounded_loop_exceeded": "infra_bug",
        "deb_budget_hard_capped": "terminal_post_send",
        "spend_cap": "terminal_post_send",
        "crashed_after_send": "terminal_post_send",
        "newer_inbound_before_final_send": "terminal_post_send",
        "submit_missing": "infra_bug",
        "deb_tool_failure": "infra_bug",
        "background_task_crashed": "infra_bug",
    }
    return _MAP.get(failure_reason, "infra_bug")


@router.get("/api/live/ops/sessions/{session_id}/debug")
async def ops_debug_session(
    session_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Operator debug endpoint — returns full session introspection.

    Returns conversation metadata, bot_turns, transcript_turns (separate
    key per SD3), artifacts grouped by type/revision with current/deleted
    markers, provenance links with durable write counts, and extracted
    failure classes.

    Operator-only: requires an authenticated caller whose id is in the
    ``live_voice_ops_user_ids`` allow-list (no-op when auth is disabled).
    """
    _require_operator(user_id)
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")

    # ── 1. Conversation metadata ──────────────────────────────────────────
    conv = await pool.fetchrow(
        "SELECT * FROM mediator.conversations WHERE id = $1",
        session_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="session not found")

    conv_dict = dict(conv)
    status = canonicalize_status(conv_dict.get("status", ""))

    conversation: dict[str, Any] = {
        "id": str(conv_dict["id"]),
        "status": status,
        "bot_id": conv_dict.get("bot_id"),
        "user_id": str(conv_dict["user_id"]) if conv_dict.get("user_id") else None,
        "partner_user_id": (
            str(conv_dict["partner_user_id"])
            if conv_dict.get("partner_user_id")
            else None
        ),
        "mode": conv_dict.get("mode"),
        "steering_text": conv_dict.get("steering_text"),
        "prep_summary": conv_dict.get("prep_summary"),
        "current_item_id": (
            str(conv_dict["current_item_id"])
            if conv_dict.get("current_item_id")
            else None
        ),
        "started_at": str(conv_dict["started_at"]) if conv_dict.get("started_at") else None,
        "ended_at": str(conv_dict["ended_at"]) if conv_dict.get("ended_at") else None,
        "created_at": str(conv_dict["created_at"]) if conv_dict.get("created_at") else None,
        "session_fields": conv_dict.get("session_fields"),
        "topic_id": str(conv_dict["topic_id"]) if conv_dict.get("topic_id") else None,
        "spend_usd_cents": conv_dict.get("spend_usd_cents"),
    }

    # ── 2. bot_turns (found by conversation_id) ───────────────────────────
    bot_turn_rows = await pool.fetch(
        """
        SELECT id, kind, model_version, failure_reason, completed_at,
               started_at, tool_call_count, duration_ms
        FROM mediator.bot_turns
        WHERE conversation_id = $1
        ORDER BY started_at
        """,
        session_id,
    )

    bot_turns: list[dict[str, Any]] = []
    for row in bot_turn_rows:
        model = row.get("model_version") or ""
        provider = model.split("/")[0] if "/" in model else ""
        bot_turns.append({
            "id": str(row["id"]),
            "kind": row.get("kind"),
            "turn_id": str(row["id"]),
            "model": model,
            "provider": provider,
            "failure_reason": row.get("failure_reason"),
            "completed": row.get("completed_at") is not None,
            "completed_at": (
                str(row["completed_at"]) if row.get("completed_at") else None
            ),
            "started_at": (
                str(row["started_at"]) if row.get("started_at") else None
            ),
            "tool_call_count": row.get("tool_call_count", 0),
            "duration_ms": row.get("duration_ms"),
        })

    # ── 3. transcript_turns (separate key per gate SD3) ────────────────────
    transcript_rows = await pool.fetch(
        """
        SELECT id, speaker_label, speaker_role, text, ts,
               asr_confidence, active_item_id, was_routing_input
        FROM mediator.transcript_turns
        WHERE conversation_id = $1
        ORDER BY ts
        """,
        session_id,
    )

    transcript_turns: list[dict[str, Any]] = []
    for row in transcript_rows:
        transcript_turns.append({
            "id": str(row["id"]),
            "speaker_label": row["speaker_label"],
            "speaker_role": row["speaker_role"],
            "text": row["text"],
            "ts": str(row["ts"]) if row.get("ts") else None,
            "asr_confidence": row.get("asr_confidence"),
            "active_item_id": (
                str(row["active_item_id"]) if row.get("active_item_id") else None
            ),
            "was_routing_input": row.get("was_routing_input", False),
        })

    # ── 4. Artifacts grouped by type/revision ──────────────────────────────
    artifact_rows = await pool.fetch(
        """
        SELECT * FROM mediator.conversation_artifacts
        WHERE conversation_id = $1
        ORDER BY artifact_type, revision_number DESC
        """,
        session_id,
    )

    # Determine current (highest non-deleted revision) per type.
    current_by_type: dict[str, int] = {}
    for row in artifact_rows:
        atype = row["artifact_type"]
        if row.get("deleted_at") is None:
            rev = row["revision_number"]
            if atype not in current_by_type or rev > current_by_type[atype]:
                current_by_type[atype] = rev

    artifacts_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in artifact_rows:
        atype = row["artifact_type"]
        is_current = (
            row.get("deleted_at") is None
            and row["revision_number"] == current_by_type.get(atype)
        )
        entry: dict[str, Any] = {
            "id": str(row["id"]),
            "artifact_type": atype,
            "revision_number": row["revision_number"],
            "payload": row.get("payload"),
            "payload_version": row.get("payload_version", 1),
            "created_by_turn_id": (
                str(row["created_by_turn_id"])
                if row.get("created_by_turn_id")
                else None
            ),
            "deleted_at": str(row["deleted_at"]) if row.get("deleted_at") else None,
            "created_at": str(row["created_at"]) if row.get("created_at") else None,
            "expires_at": str(row["expires_at"]) if row.get("expires_at") else None,
            "current": is_current,
            "deleted": row.get("deleted_at") is not None,
            "links": [],
        }
        artifacts_by_type.setdefault(atype, []).append(entry)

    # ── 5. Artifact links (provenance) ─────────────────────────────────────
    artifact_ids = [str(r["id"]) for r in artifact_rows]
    link_rows: list[Any] = []
    if artifact_ids:
        link_rows = await pool.fetch(
            """
            SELECT * FROM mediator.artifact_links
            WHERE artifact_id = ANY($1::uuid[])
            ORDER BY created_at
            """,
            artifact_ids,
        )

    provenance_links: list[dict[str, Any]] = []
    durable_counts: dict[str, int] = {}
    link_index: dict[str, list[dict[str, Any]]] = {}

    for lr in link_rows:
        link = {
            "id": str(lr["id"]),
            "artifact_id": str(lr["artifact_id"]),
            "target_table": lr["target_table"],
            "target_id": str(lr["target_id"]),
            "relation": lr["relation"],
            "evidence": lr.get("evidence"),
            "deleted_at": str(lr["deleted_at"]) if lr.get("deleted_at") else None,
            "created_at": str(lr["created_at"]) if lr.get("created_at") else None,
        }
        provenance_links.append(link)

        aid = str(lr["artifact_id"])
        link_index.setdefault(aid, []).append(link)

        # Aggregate durable write counts: count links to non-conversation-scoped
        # tables (everything except conversations, conversation_items,
        # transcript_turns, conversation_notes).
        target = lr["target_table"]
        if target not in (
            "conversations",
            "conversation_items",
            "transcript_turns",
            "conversation_notes",
        ):
            durable_counts[target] = durable_counts.get(target, 0) + 1

    # Attach links to their artifacts.
    for entries in artifacts_by_type.values():
        for entry in entries:
            entry["links"] = link_index.get(entry["id"], [])

    # ── 6. Failure classes ─────────────────────────────────────────────────
    failure_classes: dict[str, Any] = {
        "session": {},
        "bot_turns": [],
        "non_chat": [],
    }

    # From session_fields.
    sf = conv_dict.get("session_fields") or {}
    if isinstance(sf, dict):
        if sf.get("prep_error"):
            failure_classes["session"]["prep_error"] = sf["prep_error"]
        if sf.get("debrief_error") or sf.get("debrief_failure_reason"):
            failure_classes["session"]["debrief_error"] = (
                sf.get("debrief_error") or sf.get("debrief_failure_reason")
            )
        if sf.get("failure_class"):
            failure_classes["session"]["failure_class"] = sf["failure_class"]

    # From bot_turns.
    for row in bot_turn_rows:
        fr = row.get("failure_reason")
        if fr:
            failure_classes["bot_turns"].append({
                "turn_id": str(row["id"]),
                "kind": row.get("kind"),
                "failure_reason": fr,
                "failure_class": _classify_failure(fr),
            })

    # Non-chat result metadata: bot_turns created by live prep/debrief jobs
    # (identified by `kind` and no `triggered_by_message_id`).
    # These are already included in bot_turns above; surface them under a
    # separate key for discoverability.
    for row in bot_turn_rows:
        if row.get("kind") in ("live_prep", "live_debrief"):
            fr = row.get("failure_reason")
            entry: dict[str, Any] = {
                "turn_id": str(row["id"]),
                "kind": row.get("kind"),
            }
            if fr:
                entry["failure_reason"] = fr
                entry["failure_class"] = _classify_failure(fr)
            else:
                entry["outcome"] = "success"
            failure_classes["non_chat"].append(entry)

    return {
        "session_id": str(session_id),
        "conversation": conversation,
        "bot_turns": bot_turns,
        "transcript_turns": transcript_turns,
        "artifacts": artifacts_by_type,
        "provenance": {
            "links": provenance_links,
            "durable_write_counts": durable_counts,
        },
        "failure_classes": failure_classes,
    }


@router.post("/api/live/sessions/{session_id}/replay/{turn_id}")
async def replay_turn(
    session_id: UUID,
    turn_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Re-run the turn caller against the original user input.

    Useful for debugging: an operator picks a transcript_turns row that
    was the user's most-recent utterance leading into a bot turn, and
    this endpoint replays that input through ``select_turn_caller()``
    with the *current* agenda context (not the at-the-time snapshot).
    It does NOT mutate any rows — emission is returned to the caller
    only.  Apply via the WS for live updates.
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)
    user_turn = await pool.fetchrow(
        """
        SELECT text FROM mediator.transcript_turns
        WHERE id = $1 AND conversation_id = $2 AND speaker_role = 'primary'
        """,
        turn_id,
        session_id,
    )
    if user_turn is None:
        raise HTTPException(status_code=404, detail="user transcript turn not found")
    ctx = await load_turn_context(pool, session_id)
    caller = select_turn_caller()
    try:
        emission = await caller.call(
            TurnRequest(session_id=str(session_id), user_transcript_final=user_turn["text"]),
            ctx,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"replay failed: {exc}") from exc
    return {
        "ok": True,
        "input": user_turn["text"],
        "emission": emission.model_dump(),
        "caller": type(caller).__name__,
    }


@router.get("/api/live/sessions/{session_id}/tts/{turn_id}")
async def stream_tts(
    session_id: UUID,
    turn_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
):
    """Stream mp3 TTS audio for a previously emitted bot turn.

    The bot_turn WS event includes `tts_url` when a TTS provider is
    configured.  Returns 404 if there's no matching bot turn.  Falls
    through to an empty chunked response on stub provider so the
    browser still gets a clean 200 (the frontend then plays via
    SpeechSynthesis as a fallback).
    """
    from starlette.responses import StreamingResponse

    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)
    turn = await pool.fetchrow(
        """
        SELECT text FROM mediator.transcript_turns
        WHERE id = $1 AND conversation_id = $2 AND speaker_role = 'bot'
        """,
        turn_id,
        session_id,
    )
    if turn is None:
        raise HTTPException(status_code=404, detail="bot turn not found")

    provider = select_tts_provider()
    text = turn["text"]

    async def iter_chunks():
        try:
            async for chunk in provider.synthesize_mp3(text):
                yield chunk
        except Exception:
            logger.warning("tts: stream crashed", exc_info=True)
            return

    return StreamingResponse(
        iter_chunks(),
        media_type="audio/mpeg",
        headers={"X-TTS-Provider": provider.name},
    )


@router.get("/api/live/sessions/{session_id}")
async def get_session(
    session_id: UUID,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_current_user),
) -> dict[str, Any]:
    """Return a single conversation row (or 404)."""
    if not await _conversations_table_exists(pool):
        raise HTTPException(
            status_code=503,
            detail="live conversations not yet migrated",
        )
    if get_settings().live_voice_auth_enabled:
        await _require_ownership(pool, session_id, user_id)
    try:
        row = await pool.fetchrow(
            "SELECT * FROM mediator.conversations WHERE id = $1",
            session_id,
        )
    except Exception as exc:
        logger.exception("live_voice: failed to fetch conversation row")
        raise HTTPException(
            status_code=500,
            detail=f"failed to fetch live session: {exc}",
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return normalize_row_status(dict(row))


# ── WebSocket stub ───────────────────────────────────────────────────────────


@router.websocket("/ws/live/{session_id}")
async def live_socket(websocket: WebSocket, session_id: str) -> None:
    # Per-IP rate limit (10/min default). Headers in this order:
    # X-Forwarded-For (Railway / proxies), client.host fallback.
    fwd = websocket.headers.get("x-forwarded-for", "")
    client_ip = fwd.split(",")[0].strip() if fwd else (websocket.client.host if websocket.client else "unknown")
    if not WS_RATE_LIMITER.allow(client_ip):
        await websocket.close(code=4429)
        logger.warning(
            "live_voice: WS rate-limited",
            extra={"client_ip": client_ip, "session_id": session_id},
        )
        return

    # Magic-link JWT auth: token=… query param.  When
    # LIVE_VOICE_WS_AUTH_REQUIRED=1 we refuse the upgrade on missing/expired
    # tokens; when unset (the local dev default) we still verify tokens
    # when present but allow tokenless connections so the dev flow keeps
    # working until the frontend wires the magic-link DM path.
    require_auth = get_settings().live_voice_auth_enabled or (os.environ.get("LIVE_VOICE_WS_AUTH_REQUIRED") or "").strip() == "1"
    token = websocket.query_params.get("token") or ""
    authed_user_id: str | None = None
    if token:
        try:
            claims = live_jwt.verify(token)
            authed_user_id = claims.user_id
        except Exception as exc:
            logger.warning(
                "live_voice: WS bad token (%s)", exc,
                extra={"session_id": session_id, "client_ip": client_ip},
            )
            await websocket.close(code=4401)
            return
    elif require_auth:
        logger.warning(
            "live_voice: WS missing token (auth required)",
            extra={"session_id": session_id, "client_ip": client_ip},
        )
        await websocket.close(code=4401)
        return

    logger.info(
        "live_voice: WS accepted",
        extra={
            "session_id": session_id,
            "client_ip": client_ip,
            "user_id": authed_user_id,
        },
    )
    _record_event("ws_open")
    """Sprint 1+2 WS handler.

    On connect: stream phase descriptors (``Catching up…`` → ``Thinking…``
    → ``Getting ready…`` → ``ready``), then open a
    :class:`~app.services.live.stt.StreamingTranscriber` (real or stub
    based on env). Binary frames are pushed through to the transcriber;
    its events (partial / final / error) are forwarded to the client AND
    every ``final`` is persisted to ``mediator.transcript_turns``.

    Text control frames:

    * ``{"type": "end_session"}`` — clean close.
    * ``{"type": "advance"}`` — push the agenda forward (full
      ``current_item_id`` advance lands with Haiku turns).
    """
    import asyncio
    import json

    await websocket.accept()
    try:
        pool = websocket.app.state.pool

        # Ownership check: verify the authed user owns or is a partner of this
        # conversation before touching any state.  The JWT authed_user_id is a
        # str; asyncpg returns uuid.UUID objects — must coerce to UUID first or
        # the comparison always returns NotImplemented (silently rejects owners).
        ws_user_uuid: UUID | None = None
        if authed_user_id is not None:
            ws_user_uuid = UUID(authed_user_id)
            ownership_row = await pool.fetchrow(
                "SELECT user_id, partner_user_id FROM mediator.conversations WHERE id=$1::uuid",
                session_id,
            )
            if (
                ownership_row is None
                or (
                    ownership_row["user_id"] != ws_user_uuid
                    and (
                        ownership_row["partner_user_id"] is None
                        or ownership_row["partner_user_id"] != ws_user_uuid
                    )
                )
            ):
                await websocket.close(code=4003)
                return

        # ── Transition session from 'ready' → 'active' (canonical) ──────────
        # This replaces any legacy 'live' status and stamps started_at.
        # When a user is authenticated, scope the UPDATE to rows they own so an
        # authed caller cannot flip another user's session to 'active'.
        if ws_user_uuid is not None:
            await pool.execute(
                """
                UPDATE mediator.conversations
                SET status = 'active',
                    started_at = COALESCE(started_at, now())
                WHERE id = $1::uuid
                  AND status IN ('ready', 'live')
                  AND (user_id = $2 OR partner_user_id = $2)
                """,
                session_id,
                ws_user_uuid,
            )
        else:
            await pool.execute(
                """
                UPDATE mediator.conversations
                SET status = 'active',
                    started_at = COALESCE(started_at, now())
                WHERE id = $1::uuid
                  AND status IN ('ready', 'live')
                """,
                session_id,
            )
        logger.info(
            "live_voice: WS start — set status=active for session_id=%s",
            session_id,
        )

        # Streaming phase descriptors so the user sees motion while the
        # backend is "waking up".
        for label in (
            "Catching up on where you are…",
            "Thinking about what to focus on…",
            "Getting ready for our chat…",
        ):
            await websocket.send_json(
                {"type": "phase", "label": label, "session_id": session_id}
            )
            await asyncio.sleep(0.6)
        await websocket.send_json(
            {"type": "ready", "label": "Ready when you are.", "session_id": session_id}
        )

        # Open the transcriber.  Stub or real chosen at module level.
        transcriber = select_transcriber()
        await transcriber.start()

        pool = websocket.app.state.pool

        turn_caller = select_turn_caller()

        async def record_latency(conv_id: str, turn_index: int, stage: str, ms: int) -> None:
            try:
                await pool.execute(
                    """
                    INSERT INTO mediator.live_session_latency
                        (conversation_id, turn_index, stage, elapsed_ms)
                    VALUES ($1::uuid, $2, $3, $4)
                    """,
                    conv_id,
                    turn_index,
                    stage,
                    int(max(0, ms)),
                )
            except Exception:
                logger.warning("live_voice: failed to record latency span", exc_info=True)

        async def forward_events() -> None:
            from time import perf_counter
            from uuid import UUID as _UUID
            session_uuid = _UUID(session_id)
            turn_index = 0
            while True:
                event = await transcriber.events.get()
                etype = event.get("type")
                if etype == "final":
                    text = (event.get("text") or "").strip()
                    if text:
                        try:
                            await pool.execute(
                                """
                                INSERT INTO mediator.transcript_turns
                                    (conversation_id, speaker_label, speaker_role, text)
                                VALUES ($1::uuid, $2, 'primary', $3)
                                """,
                                session_id,
                                "speaker_0",
                                text,
                            )
                        except Exception:
                            logger.warning(
                                "live_voice: failed to persist transcript_turn", exc_info=True
                            )
                # Forward STT event to client regardless.
                await websocket.send_json({"type": f"transcript_{etype}", **{
                    k: v for k, v in event.items() if k != "type"
                }})

                if etype == "final" and (event.get("text") or "").strip():
                    # Budget gate: if the session has hit the hard cap we
                    # refuse to spawn a new bot turn (but still persist
                    # the user transcript and let them keep talking).
                    state = await check_budget(pool, session_uuid)
                    if state.hard_capped:
                        await websocket.send_json({
                            "type": "budget_hard_capped",
                            "cents": state.cents,
                            "hard_cap_cents": HARD_CAP_CENTS,
                        })
                        continue
                    if state.soft_warned:
                        await websocket.send_json({
                            "type": "budget_soft_warned",
                            "cents": state.cents,
                            "soft_cap_cents": SOFT_CAP_CENTS,
                            "hard_cap_cents": HARD_CAP_CENTS,
                        })
                    turn_index += 1
                    ear_to_ear_start = perf_counter()
                    asr_finalize_ms = 0
                    # Crisis classifier first — if the user said something
                    # that meets crisis criteria we drop the coach role
                    # entirely (per crisis_solo.SOLO_CRISIS_SECTION_V1).
                    user_text = event["text"]
                    try:
                        charge = await classify_charge(pool, user_text)
                    except Exception:
                        logger.warning("live_voice: charge classification crashed", exc_info=True)
                        charge = None
                    if charge is not None and charge.charge == "crisis":
                        await pool.execute(
                            """
                            INSERT INTO mediator.transcript_turns
                                (conversation_id, speaker_label, speaker_role, text)
                            VALUES ($1::uuid, 'bot', 'bot', $2)
                            """,
                            session_id,
                            _CRISIS_UTTERANCE,
                        )
                        note_text = f"[concern] crisis charge detected: {charge.reason}"
                        note_row = await pool.fetchrow(
                            """
                            INSERT INTO mediator.conversation_notes (conversation_id, text)
                            VALUES ($1, $2)
                            RETURNING id
                            """,
                            session_id,
                            note_text,
                        )
                        await enqueue_conversation_note_embed(
                            pool, note_id=note_row["id"], text=note_text,
                        )
                        await websocket.send_json({
                            "type": "bot_turn",
                            "utterance": _CRISIS_UTTERANCE,
                            "charge": "crisis",
                            "charge_reason": charge.reason,
                        })
                        continue

                    # Drive a regular bot turn off the user's final transcript.
                    llm_start = perf_counter()
                    ctx = {}
                    turn_request = TurnRequest(
                        session_id=str(session_uuid),
                        user_transcript_final=user_text,
                    )
                    try:
                        ctx = await load_turn_context(pool, session_uuid)
                        emission = await turn_caller.call(turn_request, ctx)
                    except Exception as exc:
                        logger.exception(
                            "live_voice: turn caller failed; emitting fallback reply "
                            "session_id=%s user_text=%r",
                            session_id,
                            user_text[:200],
                        )
                        emission = fallback_turn_emission(turn_request, ctx)
                        await websocket.send_json({
                            "type": "bot_turn_error",
                            "message": str(exc),
                            "fallback": True,
                        })
                    llm_ttft_ms = int((perf_counter() - llm_start) * 1000)
                    db_start = perf_counter()
                    try:
                        await apply_emission(pool, session_uuid, emission)
                    except Exception:
                        logger.warning("live_voice: apply_emission failed", exc_info=True)
                    try:
                        await pool.execute(
                            """
                            INSERT INTO mediator.transcript_turns
                                (conversation_id, speaker_label, speaker_role, text)
                            VALUES ($1::uuid, 'bot', 'bot', $2)
                            """,
                            session_id,
                            emission.utterance,
                        )
                    except Exception:
                        logger.warning("live_voice: failed to persist bot turn", exc_info=True)
                    db_ms = int((perf_counter() - db_start) * 1000)
                    ear_to_ear_ms = int((perf_counter() - ear_to_ear_start) * 1000)
                    # Look up the just-inserted bot turn id so the client
                    # can fetch TTS audio for it.
                    bot_turn_row = await pool.fetchrow(
                        """
                        SELECT id FROM mediator.transcript_turns
                        WHERE conversation_id = $1::uuid AND speaker_role = 'bot'
                        ORDER BY ts DESC LIMIT 1
                        """,
                        session_id,
                    )
                    bot_turn_id = str(bot_turn_row["id"]) if bot_turn_row else None
                    # Fire-and-forget latency persistence.
                    await record_latency(session_id, turn_index, "asr_finalize", asr_finalize_ms)
                    await record_latency(session_id, turn_index, "llm_ttft", llm_ttft_ms)
                    await record_latency(session_id, turn_index, "orchestrator_db", db_ms)
                    await record_latency(session_id, turn_index, "ear_to_ear", ear_to_ear_ms)
                    tts_url = (
                        f"/api/live/sessions/{session_id}/tts/{bot_turn_id}"
                        if bot_turn_id
                        else None
                    )
                    await websocket.send_json({
                        "type": "bot_turn",
                        "utterance": emission.utterance,
                        "route_to_item_id": emission.route_to_item_id,
                        "notes": [n.model_dump() for n in emission.notes],
                        "latency_ms": {
                            "llm_ttft": llm_ttft_ms,
                            "orchestrator_db": db_ms,
                            "ear_to_ear": ear_to_ear_ms,
                        },
                        "tts_url": tts_url,
                    })

        forwarder_task = asyncio.create_task(forward_events())

        total_frames = 0
        total_bytes = 0

        try:
            while True:
                try:
                    event = await websocket.receive()
                except WebSocketDisconnect:
                    return

                if event["type"] == "websocket.disconnect":
                    return

                data_text = event.get("text")
                data_bytes = event.get("bytes")
                if data_bytes is not None:
                    total_frames += 1
                    total_bytes += len(data_bytes)
                    await transcriber.push(data_bytes)
                    if total_frames % 8 == 0:
                        logger.info(
                            "live_voice: received audio frames session_id=%s frames=%s bytes=%s",
                            session_id,
                            total_frames,
                            total_bytes,
                        )
                        await websocket.send_json({
                            "type": "frame_ack",
                            "frames": total_frames,
                            "bytes": total_bytes,
                        })
                    continue
                if not data_text:
                    continue
                try:
                    payload = json.loads(data_text)
                except Exception:
                    await websocket.send_json({"type": "echo", "payload": data_text})
                    continue

                kind = payload.get("type") if isinstance(payload, dict) else None
                if kind == "end_session":
                    await websocket.send_json({"type": "session_ended"})
                    await websocket.close(code=1000)
                    return
                if kind == "advance":
                    await websocket.send_json({
                        "type": "phase",
                        "label": "Moving to the next focus area…",
                        "session_id": session_id,
                    })
                    continue
                if kind == "voice_active":
                    # Client VAD signaled voice. No-op on the stub
                    # transcriber; real Realtime STT uses this to start a
                    # new audio segment.
                    continue
                if kind == "turn_end":
                    # Client VAD signaled end-of-turn silence. Tell the
                    # transcriber to commit/flush the audio buffer so
                    # we get a finalized transcript NOW.
                    try:
                        await transcriber.flush()
                    except Exception:
                        logger.warning("live_voice: transcriber.flush failed", exc_info=True)
                    continue
                if kind == "barge_in":
                    # Caller is talking over the bot. Cancel the queued
                    # TTS on the client (already done locally) and stop
                    # spawning more bot turns until the user finishes.
                    await websocket.send_json({"type": "barge_in_acked"})
                    continue
                if kind == "back_up":
                    # "That's not what I meant." Rewind the most recently
                    # covered conversation_item back to 'active' so the
                    # next bot turn re-explores it.
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            target = await conn.fetchrow(
                                """
                                SELECT id FROM mediator.conversation_items
                                WHERE conversation_id = $1::uuid
                                  AND status = 'covered'
                                  AND covered_at IS NOT NULL
                                ORDER BY covered_at DESC
                                LIMIT 1
                                """,
                                session_id,
                            )
                            if target is None:
                                await websocket.send_json({
                                    "type": "back_up_acked",
                                    "rewound_item_id": None,
                                    "detail": "nothing covered yet",
                                })
                                continue
                            await conn.execute(
                                """
                                UPDATE mediator.conversation_items
                                SET status = 'active',
                                    coverage_evidence_quote = NULL,
                                    coverage_summary = NULL,
                                    covered_at = NULL
                                WHERE id = $1
                                """,
                                target["id"],
                            )
                            await conn.execute(
                                "UPDATE mediator.conversations SET current_item_id = $2 WHERE id = $1::uuid",
                                session_id,
                                target["id"],
                            )
                    await websocket.send_json({
                        "type": "back_up_acked",
                        "rewound_item_id": str(target["id"]),
                    })
                    continue
                if kind == "silence_prompt":
                    # Older clients used this as a 10s idle fallback. Do
                    # not turn silence into a fake user transcript or model
                    # turn; otherwise quiet rooms create infinite check-ins.
                    await websocket.send_json({"type": "silence_prompt_acked"})
                    continue
                if kind == "text_input":
                    # Browser dev fallback / accessibility path: the user
                    # typed a message instead of speaking. Treat it as a
                    # synthesized transcript_final, going through the
                    # same downstream loop (crisis classifier -> turn
                    # caller -> persist -> emit).
                    text = (payload.get("text") or "").strip()
                    if not text:
                        continue
                    fake_event = {"type": "final", "text": text, "ts": 0}
                    try:
                        transcriber.events.put_nowait(fake_event)
                    except Exception:
                        # Fall back to forwarding directly if the queue
                        # is full or transcriber is paused.
                        logger.warning("live_voice: failed to enqueue text_input", exc_info=True)
                    continue
                await websocket.send_json({"type": "echo", "payload": payload})
        finally:
            forwarder_task.cancel()
            await transcriber.aclose()
    except WebSocketDisconnect:
        _record_event("ws_unexpected_disconnect")
        return
    except Exception:
        _record_event("ws_5xx")
        logger.exception("live_voice: websocket handler crashed")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
