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

import logging
import os
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.bots.registry import BOT_SPECS, _maybe_register_staging_bots
from app.config import get_settings
from app.db import get_pool
from app.services.live.prep import produce_agenda, select_agenda_producer
from app.services.live.schemas import PrepRequest, TurnRequest
from app.services.live.stt import select_transcriber
from app.services.charge import classify_charge
from app.services.live.synthesis import finalize_session, save_review, synthesize_review
from app.services.live.turn_loop import apply_emission, load_turn_context, select_turn_caller
from app.services.live.tts import select_tts_provider

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


class CreateSessionResponse(BaseModel):
    session_id: UUID
    mode: str
    status: str


# ── Helpers ──────────────────────────────────────────────────────────────────


# Stable placeholder so dev runs without auth still produce a valid UUID
# in mediator.conversations.user_id (which is presumed UUID-typed).
_DEFAULT_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _resolve_test_user_id() -> UUID:
    """Read LIVE_VOICE_TEST_USER_ID from env (Settings doesn't carry it yet).

    Kept as a module-level helper so tests can monkeypatch the env var.
    """
    raw = os.environ.get("LIVE_VOICE_TEST_USER_ID", "").strip()
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


@router.get("/api/live/personas")
async def list_personas(pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Return personas the caller may steer.

    Scoped to the caller's rows in ``bot_bindings`` (joined through
    ``dyad_members`` when the binding is dyadic). When no bindings match
    (fresh install / unauthed dev), returns the full ``BOT_SPECS`` registry
    with ``scoped=false`` so the frontend can surface a "dev mode" hint.
    """
    _maybe_register_staging_bots()
    user_id = _resolve_test_user_id()  # replaced by auth.uid() when OAuth lands

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


@router.post("/api/live/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    pool: Any = Depends(get_pool),
) -> CreateSessionResponse:
    """Create a new live-voice conversation + run prep (Sprint 1).

    Calls :func:`app.services.live.prep.produce_agenda`, which inserts the
    ``mediator.conversations`` row plus its ``conversation_items`` agenda
    in a single transaction and seeds ``current_item_id``.

    Sprint 1 uses :class:`StubAgendaProducer` (deterministic, no LLM key
    required). Sprint 1b swaps in the real Anthropic Opus producer — same
    call site, only the producer changes.
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

    user_id = _resolve_test_user_id()  # TODO: replace with auth.uid() once magic-link lands.
    request = PrepRequest(
        user_id=str(user_id),
        bot_id=body.bot_id,
        steering_text=body.steering_text,
        topic_slug=body.topic,
    )
    try:
        result = await produce_agenda(pool, request, producer=select_agenda_producer())
    except Exception as exc:
        logger.exception("live_voice: prep failed")
        raise HTTPException(
            status_code=500,
            detail=f"failed to prep live session: {exc}",
        ) from exc

    mode = "steered" if (body.steering_text or "").strip() else "open"
    return CreateSessionResponse(
        session_id=UUID(result.session_id),
        mode=mode,
        status="ready",
    )


@router.get("/api/live/sessions/{session_id}/card")
async def get_session_card(session_id: UUID, pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Return the session card payload: prep_summary + items grouped by theme.

    The session card is what the user sees before pressing Start; the raw
    agenda is never exposed. See ``docs/live-conversation-mode.md`` §UI.
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")

    conv = await pool.fetchrow(
        """
        SELECT id, user_id, bot_id, mode, status, prep_summary,
               current_item_id, started_at
        FROM mediator.conversations
        WHERE id = $1
        """,
        session_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="session not found")

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
        "status": conv["status"],
        "prep_summary": conv["prep_summary"],
        "current_item_id": str(conv["current_item_id"]) if conv["current_item_id"] else None,
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
) -> dict[str, Any]:
    """Record the pre-mic consent decision.

    Writes a conversation_consent_events row for the primary speaker
    (granted) and, when partner_present, a second row for the partner
    keyed on the partner_label. Both rows are atomic under one txn so
    "consent recorded" implies "ready to open the mic".
    """
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
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
async def end_session(session_id: UUID, pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Flip the session to review_pending and synthesize the review payload."""
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    await finalize_session(pool, session_id)
    return await synthesize_review(pool, session_id)


@router.get("/api/live/sessions/{session_id}/review")
async def get_review(session_id: UUID, pool: Any = Depends(get_pool)) -> dict[str, Any]:
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    return await synthesize_review(pool, session_id)


class SaveReviewBody(BaseModel):
    model_config = {"extra": "ignore"}
    keep_items: list[dict[str, Any]] = Field(default_factory=list)
    keep_notes: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/api/live/sessions/{session_id}/review/save")
async def save_review_endpoint(
    session_id: UUID,
    body: SaveReviewBody,
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    if not await _conversations_table_exists(pool):
        raise HTTPException(status_code=503, detail="live conversations not yet migrated")
    counts = await save_review(
        pool,
        session_id,
        keep_items=body.keep_items,
        keep_notes=body.keep_notes,
    )
    return {"ok": True, "status": "synthesized", "counts": counts}


@router.get("/api/live/sessions/{session_id}/tts/{turn_id}")
async def stream_tts(session_id: UUID, turn_id: UUID, pool: Any = Depends(get_pool)):
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
async def get_session(session_id: UUID, pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Return a single conversation row (or 404)."""
    if not await _conversations_table_exists(pool):
        raise HTTPException(
            status_code=503,
            detail="live conversations not yet migrated",
        )
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
    return {key: value for key, value in dict(row).items()}


# ── WebSocket stub ───────────────────────────────────────────────────────────


@router.websocket("/ws/live/{session_id}")
async def live_socket(websocket: WebSocket, session_id: str) -> None:
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
                        await pool.execute(
                            """
                            INSERT INTO mediator.conversation_notes (conversation_id, text)
                            VALUES ($1, $2)
                            """,
                            session_id,
                            f"[concern] crisis charge detected: {charge.reason}",
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
                    try:
                        ctx = await load_turn_context(pool, session_uuid)
                        emission = await turn_caller.call(
                            TurnRequest(
                                session_id=str(session_uuid),
                                user_transcript_final=user_text,
                            ),
                            ctx,
                        )
                    except Exception as exc:
                        await websocket.send_json({
                            "type": "bot_turn_error",
                            "message": str(exc),
                        })
                        continue
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
                    # Client VAD signaled end-of-turn silence. Currently a
                    # hint only — the StubTranscriber emits on its own
                    # timer. When real STT is wired we'll forward this as
                    # an explicit `input_audio_buffer.commit` to OpenAI.
                    continue
                if kind == "barge_in":
                    # Caller is talking over the bot. Cancel the queued
                    # TTS on the client (already done locally) and stop
                    # spawning more bot turns until the user finishes.
                    await websocket.send_json({"type": "barge_in_acked"})
                    continue
                if kind == "silence_prompt":
                    # Frontend's 10s silence fallback — feed a gentle
                    # check-in through the same downstream loop.  The
                    # bot will produce a regular turn off this prompt
                    # (no special-case scripted line; let Haiku/stub
                    # respond in-character).
                    fake_event = {
                        "type": "final",
                        "text": "(silence — checking in)",
                        "ts": 0,
                    }
                    try:
                        transcriber.events.put_nowait(fake_event)
                    except Exception:
                        pass
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
        return
    except Exception:
        logger.exception("live_voice: websocket handler crashed")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
