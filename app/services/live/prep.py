"""Sprint 1 — Opus-driven prep step.

Produces a structured agenda (see :class:`app.services.live.schemas.Agenda`)
for a chosen bot + user, validates it against the schema, then persists the
session envelope to ``mediator.conversations`` and the items to
``mediator.conversation_items``.

The LLM call is abstracted behind :class:`AgendaProducer`. Production wires
this to Anthropic Opus via function calling; tests inject a stub. Both keep
the call site identical — schema validation is the gate, not the source.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.services.live.schemas import (
    Agenda,
    AgendaItem,
    PrepRequest,
    PrepResult,
)

logger = logging.getLogger(__name__)


class AgendaProducer(Protocol):
    """Anything that turns a :class:`PrepRequest` into a validated :class:`Agenda`.

    Real impls call Anthropic with prompt-cached system + tool definition;
    test impls return canned fixtures.  Both must return a model that has
    already passed :class:`Agenda` validation — this protocol is purely a
    boundary marker, not a behavior contract.
    """

    async def __call__(self, request: PrepRequest, context: dict[str, Any]) -> Agenda: ...


async def gather_prep_context(pool: Any, user_id: UUID, bot_id: str) -> dict[str, Any]:
    """Collect the inputs Opus needs to build a useful agenda.

    Pulls:
    * user record (timezone, style_notes)
    * bot binding (confirm the caller actually owns this bot)
    * recent distillations for the user+bot scope (last 20)
    * existing themes (so the agenda can cluster under them)

    Returns a dict suitable for passing to the AgendaProducer. Failures in
    any individual section are non-fatal — Opus can plan without the full
    context, just less well.
    """
    context: dict[str, Any] = {"user_id": str(user_id), "bot_id": bot_id}

    try:
        user_row = await pool.fetchrow(
            "SELECT id, name, timezone, style_notes FROM users WHERE id = $1",
            user_id,
        )
        if user_row is not None:
            context["user"] = {
                "name": user_row["name"],
                "timezone": user_row["timezone"],
                "style_notes": user_row["style_notes"],
            }
    except Exception:
        logger.warning("prep: failed to load user record", exc_info=True)

    try:
        themes = await pool.fetch(
            """
            SELECT id, slug, label
            FROM themes
            WHERE user_id = $1 AND bot_id = $2
            ORDER BY updated_at DESC NULLS LAST, created_at DESC
            LIMIT 20
            """,
            user_id,
            bot_id,
        )
        context["themes"] = [
            {"id": str(t["id"]), "slug": t["slug"], "label": t["label"]} for t in themes
        ]
    except Exception:
        logger.info("prep: themes table not queryable for this user/bot", exc_info=True)
        context["themes"] = []

    try:
        distillations = await pool.fetch(
            """
            SELECT id, content, kind, theme_id, created_at
            FROM distillations
            WHERE user_id = $1 AND bot_id = $2
            ORDER BY created_at DESC
            LIMIT 20
            """,
            user_id,
            bot_id,
        )
        context["distillations"] = [
            {
                "id": str(d["id"]),
                "content": d["content"],
                "kind": d.get("kind"),
                "theme_id": str(d["theme_id"]) if d["theme_id"] else None,
            }
            for d in distillations
        ]
    except Exception:
        logger.info("prep: distillations not queryable for this user/bot", exc_info=True)
        context["distillations"] = []

    return context


async def produce_agenda(
    pool: Any,
    request: PrepRequest,
    *,
    producer: AgendaProducer,
) -> PrepResult:
    """End-to-end prep: gather context, call producer, persist atomically.

    Persists to ``mediator.conversations`` + ``mediator.conversation_items``
    in a single transaction so a partial agenda never lands. Sets
    ``current_item_id`` on the conversation row to the UUID of the row
    matching ``agenda.first_item_id``.
    """
    user_uuid = UUID(request.user_id)
    context = await gather_prep_context(pool, user_uuid, request.bot_id)

    agenda = await producer(request, context)  # already schema-validated by the caller

    # Resolve theme_slug -> theme_id lookups up-front so the transaction is
    # short. Themes not present are silently dropped (Opus may invent slugs).
    theme_slugs = {item.theme_slug for item in agenda.items if item.theme_slug}
    theme_id_by_slug: dict[str, UUID] = {}
    if theme_slugs:
        try:
            rows = await pool.fetch(
                "SELECT id, slug FROM themes WHERE user_id = $1 AND slug = ANY($2::text[])",
                user_uuid,
                list(theme_slugs),
            )
            theme_id_by_slug = {r["slug"]: r["id"] for r in rows}
        except Exception:
            logger.warning("prep: theme lookup failed; theme_id=NULL on every item", exc_info=True)

    session_id = uuid4()
    item_uuid_by_id: dict[str, UUID] = {item.id: uuid4() for item in agenda.items}
    current_item_uuid = item_uuid_by_id[agenda.first_item_id]
    mode = "steered" if (request.steering_text or "").strip() else "open"

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO mediator.conversations
                    (id, user_id, bot_id, mode, steering_text, status, prep_summary, current_item_id)
                VALUES ($1, $2, $3, $4, $5, 'ready', $6, NULL)
                """,
                session_id,
                user_uuid,
                request.bot_id,
                mode,
                request.steering_text,
                agenda.prep_summary,
            )
            for order_hint, item in enumerate(agenda.items):
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_items (
                        id, conversation_id, theme_id, kind, title, intent, ask,
                        done_when, next_item_ids, priority, speaker_scope,
                        coverage_evidence_required, order_hint
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid[], $10, $11, $12, $13)
                    """,
                    item_uuid_by_id[item.id],
                    session_id,
                    theme_id_by_slug.get(item.theme_slug) if item.theme_slug else None,
                    item.kind,
                    item.title,
                    item.intent,
                    item.ask,
                    item.done_when,
                    [item_uuid_by_id[ref] for ref in item.next_item_ids],
                    item.priority,
                    item.speaker_scope,
                    item.coverage_evidence_required,
                    item.order_hint or order_hint,
                )
            # Now that all items exist, set current_item_id on the conversation row.
            await conn.execute(
                "UPDATE mediator.conversations SET current_item_id = $1 WHERE id = $2",
                current_item_uuid,
                session_id,
            )

    return PrepResult(
        session_id=str(session_id),
        agenda=agenda,
        items_persisted=len(agenda.items),
        current_item_id=str(current_item_uuid),
    )


# --------------------------------------------------------------------------- #
# Reference impl: a stub producer that returns a deterministic agenda based
# on the steering_text.  Used for tests AND as the v0 "no Anthropic key"
# fallback so Sprint 1 can be exercised end-to-end before live calls land.
# --------------------------------------------------------------------------- #


class StubAgendaProducer:
    """Deterministic agenda producer for tests + dev runs without an LLM key.

    Returns a 3-item agenda (one 'must', two 'should') that exercises the
    full schema: themes if any are in context, internal refs, partner scope.
    """

    async def __call__(self, request: PrepRequest, context: dict[str, Any]) -> Agenda:
        steering = (request.steering_text or "").strip() or "general check-in"
        themes = context.get("themes") or []
        first_theme_slug = themes[0]["slug"] if themes else None

        items = [
            AgendaItem(
                id="must_anchor",
                title=f"Open with what's on your mind about: {steering[:80]}",
                intent="Set the focus quickly so we don't drift.",
                ask="In one sentence, what's the thing you most want to talk about?",
                done_when="The user names a concrete topic or moment.",
                kind="planned",
                priority="must",
                speaker_scope="primary",
                coverage_evidence_required="explicit_answer",
                next_item_ids=["should_context", "should_outcome"],
                theme_slug=first_theme_slug,
                order_hint=0,
            ),
            AgendaItem(
                id="should_context",
                title="What context matters here?",
                intent="Surface the surrounding facts before jumping to conclusions.",
                ask="What happened just before this came up for you?",
                done_when="A short scene or trigger has been described.",
                kind="planned",
                priority="should",
                speaker_scope="primary",
                coverage_evidence_required="explicit_answer",
                order_hint=1,
            ),
            AgendaItem(
                id="should_outcome",
                title="What would 'handled' look like by the end?",
                intent="Make the success criterion concrete so we know when to stop.",
                ask="If this conversation lands well, what changes for you tomorrow?",
                done_when="A concrete, observable next step or feeling has been named.",
                kind="planned",
                priority="should",
                speaker_scope="primary",
                coverage_evidence_required="concrete_decision",
                order_hint=2,
            ),
        ]
        agenda = Agenda(
            prep_summary=f"Stub agenda for steering={steering!r}; 3 items, anchored on first response.",
            items=items,
            first_item_id="must_anchor",
        )
        # Round-trip validates the schema (raises on internal-ref / uniqueness failures).
        return Agenda.model_validate(json.loads(agenda.model_dump_json()))
