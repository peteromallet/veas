"""Sprint 3 — live per-turn caller.

Contract (:class:`TurnCaller`): given a fresh user transcript, return one
:class:`~app.services.live.schemas.TurnEmission`.  The orchestrator then
applies it atomically to the DB.

Ships two impls:

* :class:`StubTurnCaller` — deterministic stub for dev/no-key runs.
  Generates a plausible selected-bot utterance, advances coverage on the
  current item, and notes a single "fact".  Wire protocol is identical to
  the real Haiku caller.
* :class:`AnthropicHaikuTurnCaller` — calls Claude Haiku 4.5 with the
  agenda prompt-cached.  Selected when ``LIVE_VOICE_TURN_PROVIDER=anthropic``.
* :class:`DeepseekTurnCaller` — calls DeepSeek JSON mode.  Used as the
  production fallback when Anthropic is present but unavailable.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services.live.schemas import (
    CoverageDelta,
    TurnEmission,
    TurnNote,
    TurnRequest,
)
from app.services.live.bot_profile import (
    format_live_bot_profile,
    live_bot_profile_context,
    user_from_live_row,
)
from app.services.message_embedding_lifecycle import (
    enqueue_conversation_note_embed,
)

logger = logging.getLogger(__name__)

_HOT_CONTEXT_CACHE_TTL_SECONDS = 120.0
_HOT_CONTEXT_MAX_CHARS = 2600
_HOT_CONTEXT_KEEP_SECTIONS = {
    "## You",
    "## Your Partner",
    "## Current time",
    "## Topic status",
    "## Upcoming reminders",
    "## Pregnancy",
    "## Partner pregnancy",
    "## Fitness",
    "## Recent messages",
}
_hot_context_cache: dict[str, tuple[float, str]] = {}


class TurnCaller(Protocol):
    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission: ...


class FallbackTurnCaller:
    """Try a primary turn caller, then a secondary caller before surfacing failure."""

    def __init__(
        self,
        primary: TurnCaller,
        fallback: TurnCaller,
        *,
        primary_name: str,
        fallback_name: str,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fallback_name = fallback_name

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        try:
            return await self.primary.call(request, context)
        except Exception as exc:
            logger.warning(
                "turn_loop: %s turn caller failed; falling back to %s: %s",
                self.primary_name,
                self.fallback_name,
                exc,
            )
            return await self.fallback.call(request, context)


def fallback_turn_emission(request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
    """Build a minimal valid live reply when the model turn fails.

    Live voice should degrade to a short spoken recovery instead of leaving the
    user staring at their own transcript. Keep this deterministic and avoid
    writing coverage, because we do not know what the failed model intended.
    """
    conv = context.get("conversation") or {}
    bot_profile = context.get("bot_profile") or {}
    bot_name = (
        bot_profile.get("display_name")
        or bot_profile.get("name")
        or conv.get("bot_id")
        or "I"
    )
    items = context.get("items") or []
    current_id = conv.get("current_item_id")
    current_ask = None
    for item in items:
        if str(item.get("id")) == str(current_id):
            current_ask = item.get("ask")
            break

    user_text = (request.user_transcript_final or "").strip()
    if user_text.lower() in {
        "hey, can you hear me?",
        "can you hear me?",
        "hello?",
        "hello",
    }:
        utterance = f"Yes, I can hear you. {current_ask or 'What would you like to focus on?'}"
    else:
        utterance = (
            f"I heard that. I hit a formatting snag on my side, so let me keep us moving: "
            f"{current_ask or 'what feels most important to say next?'}"
        )

    return TurnEmission(
        utterance=utterance,
        notes=[
            TurnNote(
                kind="concern",
                text=f"{bot_name} used live-turn fallback after model emission failure.",
            )
        ],
    )


def select_turn_caller() -> "TurnCaller":
    settings = get_settings()
    provider = (
        os.environ.get("LIVE_VOICE_TURN_PROVIDER")
        or getattr(settings, "live_voice_turn_provider", "")
        or ""
    ).strip().lower()
    anthropic_key = (
        settings.anthropic_api_key.get_secret_value()
        if settings.anthropic_api_key is not None
        else ""
    ).strip()
    deepseek_key = (
        settings.deepseek_api_key.get_secret_value()
        if settings.deepseek_api_key is not None
        else ""
    ).strip()
    has_anthropic = anthropic_key.startswith("sk-ant-") and "stub" not in anthropic_key
    has_deepseek = bool(deepseek_key) and "stub" not in deepseek_key.lower()

    if provider == "stub":
        return StubTurnCaller()
    if provider == "deepseek":
        return DeepseekTurnCaller()
    if provider == "anthropic":
        if has_deepseek:
            return FallbackTurnCaller(
                AnthropicHaikuTurnCaller(),
                DeepseekTurnCaller(),
                primary_name="anthropic",
                fallback_name="deepseek",
            )
        return AnthropicHaikuTurnCaller()
    # Auto-select: keep the designed Anthropic path, but never let an
    # unavailable Anthropic account block live replies when DeepSeek is configured.
    if has_anthropic and has_deepseek:
        return FallbackTurnCaller(
            AnthropicHaikuTurnCaller(),
            DeepseekTurnCaller(),
            primary_name="anthropic",
            fallback_name="deepseek",
        )
    if has_anthropic:
        return AnthropicHaikuTurnCaller()
    if has_deepseek:
        return DeepseekTurnCaller()
    return StubTurnCaller()


async def load_turn_context(pool: Any, session_id: UUID) -> dict[str, Any]:
    """Pull what Haiku needs to plan a turn: conversation row, current item,
    last few transcript_turns, items still pending.
    """
    context: dict[str, Any] = {"session_id": str(session_id)}
    try:
        conv = await pool.fetchrow(
            """
            SELECT id, user_id, partner_user_id, bot_id, prep_summary,
                   current_item_id, session_fields, status, topic_id
            FROM mediator.conversations
            WHERE id = $1
            """,
            session_id,
        )
    except Exception:
        logger.warning("turn_loop: failed to load conversation row", exc_info=True)
        return context
    if conv is None:
        return context
    context["conversation"] = {k: v for k, v in dict(conv).items()}
    bot_id = context["conversation"].get("bot_id")
    user_id = context["conversation"].get("user_id")

    if bot_id is not None:
        user = None
        if user_id is not None:
            try:
                user_row = await pool.fetchrow(
                    """
                    SELECT id, name, phone, timezone, onboarding_state,
                           pacing_preferences, pregnancy_edd, pregnancy_dating_basis,
                           pregnancy_lmp_date, pregnancy_scan_date,
                           pregnancy_scan_corrected_at, pregnancy_started_at,
                           pregnancy_ended_at, pregnancy_outcome
                    FROM users
                    WHERE id = $1
                    """,
                    user_id,
                )
                user = user_from_live_row(user_id, user_row)
            except Exception:
                logger.warning("turn_loop: failed to load user row", exc_info=True)
        context["bot_profile"] = live_bot_profile_context(bot_id, user=user)
        if user is not None:
            context["temporal_anchor"] = _temporal_anchor(user)
        if user is not None:
            context["hot_context_rendered"] = await _load_rendered_hot_context(
                pool,
                conv=dict(conv),
                user=user,
            )

    try:
        items = await pool.fetch(
            """
            SELECT id, title, intent, ask, done_when, status, priority, order_hint
            FROM mediator.conversation_items
            WHERE conversation_id = $1
            ORDER BY order_hint, created_at
            """,
            session_id,
        )
        context["items"] = [dict(r) for r in items]
    except Exception:
        context["items"] = []

    try:
        last_turns = await pool.fetch(
            """
            SELECT speaker_role, speaker_label, text, ts
            FROM mediator.transcript_turns
            WHERE conversation_id = $1
            ORDER BY ts DESC
            LIMIT 8
            """,
            session_id,
        )
        context["last_turns"] = list(reversed([dict(r) for r in last_turns]))
    except Exception:
        context["last_turns"] = []

    return context


def _temporal_anchor(user: Any) -> dict[str, str]:
    timezone = getattr(user, "timezone", None) or "UTC"
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        timezone = "UTC"
        tz = UTC
    local_now = datetime.now(UTC).astimezone(tz)
    return {
        "timezone": timezone,
        "local_date": local_now.date().isoformat(),
        "local_day": local_now.strftime("%A"),
        "local_time": local_now.strftime("%H:%M"),
        "iso": local_now.isoformat(),
    }


async def _load_rendered_hot_context(
    pool: Any,
    *,
    conv: dict[str, Any],
    user: Any,
) -> str | None:
    """Load the same rendered hot context used by normal chat turns.

    Live replies are intentionally lightweight, but they still need the
    selected bot's current memory / topic / adherence context.  Fail closed to
    no extra context so live voice never stops because an auxiliary read fails.
    """
    bot_id = conv.get("bot_id")
    topic_id = conv.get("topic_id")
    cache_key = str(conv.get("id") or "")
    if cache_key:
        cached = _hot_context_cache.get(cache_key)
        if cached is not None:
            expires_at, rendered = cached
            if expires_at > time.monotonic():
                return rendered
    if not bot_id or topic_id is None:
        return None
    try:
        from app.bots.registry import get_bot_spec
        from app.services.hot_context import build_hot_context, render_hot_context
        from app.services.hot_context_solo import (
            build_hot_context_solo,
            render_hot_context_solo,
        )

        bot_spec = get_bot_spec(str(bot_id))
        trigger_metadata = {
            "kind": "live_turn",
            "conversation_id": str(conv.get("id")),
            "bot_id": str(bot_id),
        }

        if bot_spec.participants_shape == "solo":
            hot_context = await build_hot_context_solo(
                pool,
                user,
                [],
                trigger_metadata,
                primary_topic_id=topic_id,
                bot_id=str(bot_id),
                allow_cross_topic_peek=getattr(
                    bot_spec.read_scopes, "allow_cross_topic_peek", False
                ),
            )
            rendered = _trim_rendered_hot_context(render_hot_context_solo(hot_context))
            if cache_key:
                _hot_context_cache[cache_key] = (
                    time.monotonic() + _HOT_CONTEXT_CACHE_TTL_SECONDS,
                    rendered,
                )
            return rendered

        partner_user_id = conv.get("partner_user_id")
        if partner_user_id is None:
            return None
        partner_row = await pool.fetchrow(
            """
            SELECT id, name, phone, timezone, onboarding_state,
                   pacing_preferences, pregnancy_edd, pregnancy_dating_basis,
                   pregnancy_lmp_date, pregnancy_scan_date,
                   pregnancy_scan_corrected_at, pregnancy_started_at,
                   pregnancy_ended_at, pregnancy_outcome
            FROM users
            WHERE id = $1
            """,
            partner_user_id,
        )
        if partner_row is None:
            return None
        partner = user_from_live_row(partner_user_id, partner_row)
        hot_context = await build_hot_context(
            pool,
            user,
            partner,
            [],
            trigger_metadata,
            primary_topic_id=topic_id,
            bot_id=bot_id,
            allow_cross_topic_peek=getattr(
                bot_spec.read_scopes, "allow_cross_topic_peek", False
            ),
            allow_cross_topic_status_injection=getattr(
                bot_spec.read_scopes,
                "allow_cross_topic_status_injection",
                False,
            ),
        )
        rendered = _trim_rendered_hot_context(render_hot_context(hot_context))
        if cache_key:
            _hot_context_cache[cache_key] = (
                time.monotonic() + _HOT_CONTEXT_CACHE_TTL_SECONDS,
                rendered,
            )
        return rendered
    except Exception:
        logger.warning(
            "turn_loop: failed to load rendered hot context for live turn",
            exc_info=True,
        )
        return None


def _trim_rendered_hot_context(rendered: str) -> str:
    """Keep live-turn grounding useful without making every voice turn huge."""
    text = (rendered or "").strip()
    sections: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_header is not None:
                sections.append((current_header, current_lines))
            current_header = line.strip()
            current_lines = [line]
        elif current_header is not None:
            current_lines.append(line)
    if current_header is not None:
        sections.append((current_header, current_lines))

    kept: list[str] = []
    for header, lines in sections:
        if header in _HOT_CONTEXT_KEEP_SECTIONS:
            kept.extend(_clip_hot_context_section(header, lines))
            kept.append("")
    trimmed = "\n".join(kept).strip() or text
    if len(trimmed) <= _HOT_CONTEXT_MAX_CHARS:
        return trimmed

    return (
        trimmed[: _HOT_CONTEXT_MAX_CHARS - 80].rstrip()
        + "\n\n[hot context clipped for live voice latency]"
    )


def _clip_hot_context_section(header: str, lines: list[str]) -> list[str]:
    """Bound verbose hot-context sections for a fast voice reply prompt."""
    if header == "## Recent messages":
        return lines[:7]
    if header == "## Fitness":
        return lines[:24]
    if header in {"## Current time", "## You", "## Your Partner"}:
        return lines[:10]
    return lines[:8]


async def apply_emission(pool: Any, session_id: UUID, emission: TurnEmission) -> None:
    """Atomic apply of a validated TurnEmission to the DB.

    * Coverage: bump conversation_items.status (+ coverage fields) for each delta.
    * new_items: insert as conversation_items with kind in {dynamic, thread}.
    * notes: insert as conversation_notes rows.
    * session_fields_patch: shallow-merge into conversations.session_fields.
    * route_to_item_id: update conversations.current_item_id.

    Maps Haiku's stable string item ids to DB UUIDs via title lookup for
    coverage on planned items (the stub uses titles); the real Haiku
    caller will be given UUIDs in its prompt so it returns them directly.
    """
    inserted_notes: list[tuple[UUID, str]] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for delta in emission.coverage:
                # Coverage targets may arrive as either a UUID string or a
                # planning-time id (e.g. "must_anchor").  Resolve via UUID
                # first, fall back to title prefix match for the stub.
                target_uuid = _maybe_uuid(delta.item_id)
                if target_uuid is None:
                    # Stub-id path: match against the title prefix that
                    # StubAgendaProducer baked in.
                    row = await conn.fetchrow(
                        """
                        SELECT id FROM mediator.conversation_items
                        WHERE conversation_id = $1 AND title ILIKE $2 || '%'
                        ORDER BY order_hint
                        LIMIT 1
                        """,
                        session_id,
                        delta.item_id[:8],  # best-effort
                    )
                    if row is None:
                        continue
                    target_uuid = row["id"]
                await conn.execute(
                    """
                    UPDATE mediator.conversation_items
                    SET status = $2,
                        coverage_evidence_quote = COALESCE($3, coverage_evidence_quote),
                        coverage_summary = COALESCE($4, coverage_summary),
                        covered_at = CASE WHEN $2 = 'covered' THEN now() ELSE covered_at END
                    WHERE id = $1
                    """,
                    target_uuid,
                    delta.status,
                    delta.evidence_quote,
                    delta.summary,
                )

            for new in emission.new_items:
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_items
                        (conversation_id, kind, title, intent, ask, done_when,
                         priority, speaker_scope, coverage_evidence_required)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    session_id,
                    new.kind,
                    new.title,
                    new.intent,
                    new.ask,
                    new.done_when,
                    new.priority,
                    new.speaker_scope,
                    new.coverage_evidence_required,
                )

            for note in emission.notes:
                # conversation_notes has no `kind` column; encode kind as a
                # short text prefix so we don't lose it.
                note_text = f"[{note.kind}] {note.text}"
                row = await conn.fetchrow(
                    """
                    INSERT INTO mediator.conversation_notes
                        (conversation_id, text)
                    VALUES ($1, $2)
                    RETURNING id
                    """,
                    session_id,
                    note_text,
                )
                inserted_notes.append((row["id"], note_text))

            if emission.session_fields_patch:
                # Shallow-merge: read current, patch, write back.
                row = await conn.fetchrow(
                    "SELECT session_fields FROM mediator.conversations WHERE id = $1",
                    session_id,
                )
                current = dict(row["session_fields"] or {}) if row else {}
                current.update(emission.session_fields_patch)
                import json as _json
                await conn.execute(
                    "UPDATE mediator.conversations SET session_fields = $2 WHERE id = $1",
                    session_id,
                    _json.dumps(current),
                )

            if emission.route_to_item_id:
                target = _maybe_uuid(emission.route_to_item_id)
                if target is not None:
                    await conn.execute(
                        "UPDATE mediator.conversations SET current_item_id = $2 WHERE id = $1",
                        session_id,
                        target,
                    )

        # Enqueue embedding lifecycle jobs after the transaction commits.
        for note_id, note_text in inserted_notes:
            await enqueue_conversation_note_embed(pool, note_id=note_id, text=note_text)


def _maybe_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Stub impl.
# --------------------------------------------------------------------------- #


class StubTurnCaller:
    """Deterministic stub used in dev / tests / no-Anthropic-key local runs."""

    def __init__(self) -> None:
        self._turn_count = 0

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        self._turn_count += 1
        items = context.get("items") or []
        current_id = (context.get("conversation") or {}).get("current_item_id")
        current_title = "this"
        next_item_id = None
        for item in items:
            if item["id"] == current_id:
                current_title = item["title"]
            if item["status"] in ("pending", "active") and str(item["id"]) != str(current_id):
                next_item_id = str(item["id"])
                break

        user_text = (request.user_transcript_final or "").strip()
        utterance = (
            f"Thanks for sharing that. I hear you saying: \"{user_text[:120]}\". "
            f"Let's stay with this for a moment before we move on."
        )
        coverage: list[CoverageDelta] = []
        if current_id:
            coverage.append(
                CoverageDelta(
                    item_id=str(current_id),
                    status="covered" if self._turn_count >= 1 else "active",
                    evidence_quote=user_text[:200] or "(no transcript)",
                    summary=f"Stub coverage update for {current_title!r}.",
                )
            )
        notes: list[TurnNote] = [
            TurnNote(kind="fact", text=f"Turn {self._turn_count}: user said {user_text[:80]!r}.")
        ]
        return TurnEmission(
            utterance=utterance,
            route_to_item_id=next_item_id,
            coverage=coverage,
            notes=notes,
        )


# --------------------------------------------------------------------------- #
# Real impl: Anthropic Haiku 4.5 with prompt-cached agenda.
# --------------------------------------------------------------------------- #


class AnthropicHaikuTurnCaller:
    """Real Haiku caller; not exercised in the stub-key local run.

    Implementation outline (left intentionally tight so it can be
    iterated against a real key):

    * Build a system prompt that loads the agenda + last_turns + current
      item details (prompt-cached via `cache_control: {type:"ephemeral"}`).
    * Tool schema: a single tool named ``emit_live_turn`` whose JSON
      schema mirrors :class:`TurnEmission`.
    * Force tool use; parse the resulting tool_use block; validate via
      :class:`TurnEmission`; return.
    """

    def __init__(self, *, model: str = "claude-haiku-4-5-20251001") -> None:
        self._model = model

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        import json
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"anthropic SDK unavailable: {exc}") from exc

        client = anthropic.AsyncAnthropic()
        conv = context.get("conversation") or {}
        bot_profile = context.get("bot_profile") or {"bot_id": conv.get("bot_id")}
        items = context.get("items") or []
        last_turns = context.get("last_turns") or []
        rendered_hot_context = (context.get("hot_context_rendered") or "").strip()
        temporal_anchor = context.get("temporal_anchor") or {}

        system = [
            {
                "type": "text",
                "text": (
                    "You are the selected Veas live-voice bot. Always respond with the "
                    "emit_live_turn tool; never use plain text. The agenda below is the "
                    "checklist you must drive. Follow the selected bot profile, scope, "
                    "and style. Stay grounded, short utterances (<= 60 words), and only "
                    "mark an item 'covered' when you can quote the user."
                ),
            },
            {
                "type": "text",
                "text": "SELECTED BOT PROFILE:\n" + format_live_bot_profile(bot_profile),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"PREP SUMMARY:\n{conv.get('prep_summary') or '(no prep summary)'}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    "TEMPORAL ANCHOR:\n"
                    f"- user_timezone: {temporal_anchor.get('timezone') or 'unknown'}\n"
                    f"- user_local_date: {temporal_anchor.get('local_date') or 'unknown'}\n"
                    f"- user_local_day: {temporal_anchor.get('local_day') or 'unknown'}\n"
                    f"- user_local_time: {temporal_anchor.get('local_time') or 'unknown'}\n\n"
                    "CURRENT CHAT-AGENT HOT CONTEXT:\n"
                    + (
                        rendered_hot_context
                        if rendered_hot_context
                        else "(hot context unavailable)"
                    )
                    + "\n\nTreat this as the source of truth for current dates, "
                    "week boundaries, commitments, adherence, memories, and "
                    "recent messages. Do not infer missed days or dates that "
                    "are not supported here; ask a clarifying question instead. "
                    "If the user seems confused about which day/week you mean, "
                    "state the exact date/day you are using before asking."
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "AGENDA:\n" + json.dumps([{
                    "id": str(i["id"]),
                    "title": i["title"],
                    "status": i["status"],
                    "priority": i["priority"],
                    "intent": i.get("intent"),
                    "ask": i.get("ask"),
                    "done_when": i.get("done_when"),
                } for i in items], indent=2),
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user_content = "RECENT TRANSCRIPT:\n" + "\n".join(
            f"- [{t['speaker_role']}] {t['text']}" for t in last_turns[-6:]
        ) + f"\n\nLATEST USER UTTERANCE:\n{request.user_transcript_final}"

        tool = {
            "name": "emit_live_turn",
            "description": "Emit exactly one structured turn output.",
            "input_schema": TurnEmission.model_json_schema(),
        }
        resp = await client.messages.create(
            model=self._model,
            max_tokens=512,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_live_turn"},
            messages=[{"role": "user", "content": user_content}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "emit_live_turn":
                return TurnEmission.model_validate(block.input)
        raise RuntimeError("Haiku did not emit a tool_use; check tool_choice settings")


# --------------------------------------------------------------------------- #
# Deepseek impl: JSON-mode chat completion (no tool_choice forcing needed).
# --------------------------------------------------------------------------- #


class DeepseekTurnCaller:
    """Deepseek per-turn caller.

    Uses Deepseek's OpenAI-compatible /chat/completions with
    ``response_format={"type":"json_object"}`` and prompt-side schema injection.
    Selected by ``LIVE_VOICE_TURN_PROVIDER=deepseek`` or auto-selected when
    a Deepseek key is present and the Anthropic key is missing/placeholder.
    """

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        import json

        import httpx

        from app.config import get_settings

        settings = get_settings()
        if settings.deepseek_api_key is None:
            raise RuntimeError("DEEPSEEK_API_KEY not configured")
        model = self._model or settings.deepseek_conversational_model

        conv = context.get("conversation") or {}
        bot_profile = context.get("bot_profile") or {"bot_id": conv.get("bot_id")}
        items = context.get("items") or []
        last_turns = context.get("last_turns") or []
        rendered_hot_context = (context.get("hot_context_rendered") or "").strip()
        temporal_anchor = context.get("temporal_anchor") or {}

        schema = TurnEmission.model_json_schema()
        agenda = [
            {
                "id": str(i["id"]),
                "title": i["title"],
                "status": i["status"],
                "priority": i["priority"],
                "intent": i.get("intent"),
                "ask": i.get("ask"),
                "done_when": i.get("done_when"),
            }
            for i in items
        ]
        system_text = (
            "You are the selected Veas live-voice bot. Respond with ONE JSON "
            "object that validates against OUTPUT_SCHEMA below; no prose, no "
            "markdown, no code fences. Follow the selected bot profile, scope, "
            "and style. Stay grounded, keep utterances <= 60 words, and only "
            "mark an item 'covered' when you can quote the user.\n\n"
            f"OUTPUT_SCHEMA:\n{json.dumps(schema)}\n\n"
            f"SELECTED BOT PROFILE:\n{format_live_bot_profile(bot_profile)}\n\n"
            f"PREP SUMMARY:\n{conv.get('prep_summary') or '(no prep summary)'}\n\n"
            "TEMPORAL ANCHOR:\n"
            f"- user_timezone: {temporal_anchor.get('timezone') or 'unknown'}\n"
            f"- user_local_date: {temporal_anchor.get('local_date') or 'unknown'}\n"
            f"- user_local_day: {temporal_anchor.get('local_day') or 'unknown'}\n"
            f"- user_local_time: {temporal_anchor.get('local_time') or 'unknown'}\n\n"
            "CURRENT CHAT-AGENT HOT CONTEXT:\n"
            + (rendered_hot_context if rendered_hot_context else "(hot context unavailable)")
            + "\n\nTreat this as the source of truth for current dates, week "
            "boundaries, commitments, adherence, memories, and recent messages. "
            "Do not infer missed days or dates that are not supported here; ask "
            "a clarifying question instead. If the user seems confused about "
            "which day/week you mean, state the exact date/day you are using "
            "before asking.\n\n"
            f"AGENDA:\n{json.dumps(agenda, indent=2)}"
        )
        user_content = (
            "RECENT TRANSCRIPT:\n"
            + "\n".join(f"- [{t['speaker_role']}] {t['text']}" for t in last_turns[-6:])
            + f"\n\nLATEST USER UTTERANCE:\n{request.user_transcript_final}"
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if settings.deepseek_reasoning_effort:
            payload["reasoning_effort"] = settings.deepseek_reasoning_effort

        async with httpx.AsyncClient(timeout=settings.provider_call_timeout_seconds) as client:
            resp = await client.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Deepseek returned unexpected payload: {data!r}") from exc
        return TurnEmission.model_validate_json(content)
