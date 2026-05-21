"""Sprint 3b — end-of-session synthesizer.

Synthesizes a 4-section review from the artifacts the live turn loop
produced:

* ``what_heard`` — short bullets summarizing the user's transcript_turns.
* ``what_decided`` — items advanced to ``status='covered'`` with a
  coverage_summary or evidence_quote.
* ``still_open`` — items still ``pending`` / ``active`` (incl. dynamic
  items the bot introduced).
* ``what_to_remember`` — conversation_notes entries flagged ``[fact]``,
  ``[open_loop]``, ``[decision]``.

The v1 synthesizer is deterministic / no-LLM so a session ending with
zero artifacts still produces a meaningful card and the e2e flow is
testable without a real key.  ``OpusSynthesizer`` is the v1.1 hook.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


_NOTE_KIND_RE = re.compile(r"^\[(?P<kind>fact|open_loop|concern|decision)\]\s*(?P<body>.*)$")


async def synthesize_review(pool: Any, session_id: UUID) -> dict[str, Any]:
    """Pure-Python synthesis: read artifacts, bucket them, return a dict."""
    conv = await pool.fetchrow(
        """
        SELECT id, bot_id, mode, status, prep_summary, started_at, ended_at,
               session_fields
        FROM mediator.conversations
        WHERE id = $1
        """,
        session_id,
    )
    if conv is None:
        return {
            "session_id": str(session_id),
            "what_heard": [],
            "what_decided": [],
            "still_open": [],
            "what_to_remember": [],
            "is_empty": True,
        }

    items = await pool.fetch(
        """
        SELECT id, title, status, priority, kind,
               coverage_summary, coverage_evidence_quote, intent
        FROM mediator.conversation_items
        WHERE conversation_id = $1
        ORDER BY order_hint, created_at
        """,
        session_id,
    )
    turns = await pool.fetch(
        """
        SELECT speaker_role, text, ts
        FROM mediator.transcript_turns
        WHERE conversation_id = $1
        ORDER BY ts
        """,
        session_id,
    )
    notes = await pool.fetch(
        """
        SELECT id, text, attributed_to_speaker, created_at
        FROM mediator.conversation_notes
        WHERE conversation_id = $1
        ORDER BY created_at
        """,
        session_id,
    )

    # what_heard: bucket the primary user's transcript by simple sentence
    # splitting, then keep the most "signal-dense" lines (>= 6 words).
    what_heard: list[str] = []
    for t in turns:
        if t["speaker_role"] != "primary":
            continue
        for sentence in re.split(r"(?<=[.?!])\s+", (t["text"] or "").strip()):
            words = sentence.strip()
            if len(words.split()) >= 4:
                what_heard.append(words)
    # Cap to last 6 to keep the card readable.
    what_heard = what_heard[-6:]

    what_decided: list[dict[str, str]] = []
    still_open: list[dict[str, str]] = []
    for item in items:
        if item["status"] == "covered":
            what_decided.append({
                "item_id": str(item["id"]),
                "title": item["title"],
                "summary": item["coverage_summary"] or "(covered)",
                "evidence_quote": item["coverage_evidence_quote"] or "",
            })
        elif item["status"] in ("pending", "active"):
            still_open.append({
                "item_id": str(item["id"]),
                "title": item["title"],
                "priority": item["priority"],
                "intent": item["intent"] or "",
            })

    what_to_remember: list[dict[str, str]] = []
    for n in notes:
        kind = "fact"
        body = (n["text"] or "").strip()
        match = _NOTE_KIND_RE.match(body)
        if match:
            kind = match.group("kind")
            body = match.group("body").strip()
        what_to_remember.append({
            "note_id": str(n["id"]),
            "kind": kind,
            "text": body,
        })

    return {
        "session_id": str(session_id),
        "bot_id": conv["bot_id"],
        "status": conv["status"],
        "started_at": (conv["started_at"].isoformat() if conv["started_at"] else None),
        "ended_at": (conv["ended_at"].isoformat() if conv["ended_at"] else None),
        "prep_summary": conv["prep_summary"],
        "what_heard": what_heard,
        "what_decided": what_decided,
        "still_open": still_open,
        "what_to_remember": what_to_remember,
        "is_empty": not (what_heard or what_decided or still_open or what_to_remember),
    }


async def finalize_session(pool: Any, session_id: UUID) -> str:
    """Mark ``conversations.ended_at`` + flip status.

    When ``live_debrief_agentic_enabled`` is True, sets status to ``debriefing``
    so the debrief background job can run.  Otherwise sets ``review_pending``.

    Returns the new status string.
    """
    from app.config import get_settings
    settings = get_settings()
    new_status = "debriefing" if settings.live_debrief_agentic_enabled else "review_pending"

    await pool.execute(
        """
        UPDATE mediator.conversations
        SET status = $2,
            ended_at = COALESCE(ended_at, now())
        WHERE id = $1
        """,
        session_id,
        new_status,
    )
    return new_status


async def save_review(
    pool: Any,
    session_id: UUID,
    *,
    keep_items: list[dict[str, Any]],
    keep_notes: list[dict[str, Any]],
) -> dict[str, int]:
    """Persist review edits + write kept notes through to ``observations``.

    Each kept (non-empty) note becomes an `observations` row attributed to
    the session's user, with `recorded_by_bot_id` set to the bot. That's
    the v1 write-through; distillations + themes follow once we have a
    real Opus synthesizer to bucket them.

    Returns a dict with the row counts written so the endpoint can echo
    them back to the client.
    """
    counts = {"items_updated": 0, "notes_updated": 0, "notes_deleted": 0, "observations_written": 0}
    async with pool.acquire() as conn:
        async with conn.transaction():
            conv = await conn.fetchrow(
                "SELECT user_id, bot_id FROM mediator.conversations WHERE id = $1",
                session_id,
            )
            if conv is None:
                raise RuntimeError(f"conversation {session_id} not found")
            user_id: UUID = conv["user_id"]
            bot_id: str | None = conv["bot_id"]

            for item in keep_items:
                if not item.get("item_id"):
                    continue
                try:
                    item_id = UUID(item["item_id"])
                except Exception:
                    continue
                summary = (item.get("summary") or "").strip() or None
                await conn.execute(
                    """
                    UPDATE mediator.conversation_items
                    SET coverage_summary = COALESCE($2, coverage_summary)
                    WHERE id = $1
                    """,
                    item_id,
                    summary,
                )
                counts["items_updated"] += 1

            for note in keep_notes:
                if not note.get("note_id"):
                    continue
                try:
                    note_id = UUID(note["note_id"])
                except Exception:
                    continue
                text = (note.get("text") or "").strip()
                if not text:
                    await conn.execute(
                        "DELETE FROM mediator.conversation_notes WHERE id = $1",
                        note_id,
                    )
                    counts["notes_deleted"] += 1
                    continue
                await conn.execute(
                    "UPDATE mediator.conversation_notes SET text = $2 WHERE id = $1",
                    note_id,
                    text,
                )
                counts["notes_updated"] += 1
                # Write-through: every kept note becomes an observation row.
                # The note text already includes the [kind] prefix, so it
                # stays grep-able in the observations stream.
                try:
                    await conn.execute(
                        """
                        INSERT INTO mediator.observations
                            (content, about_user_id, confidence, status,
                             recorded_by_bot_id)
                        VALUES ($1, $2, 'medium', 'active', $3)
                        """,
                        text,
                        user_id,
                        bot_id,
                    )
                    counts["observations_written"] += 1
                except Exception:
                    logger.warning(
                        "save_review: failed to write observation for note %s",
                        note_id,
                        exc_info=True,
                    )

            await conn.execute(
                """
                UPDATE mediator.conversations
                SET status = 'completed'
                WHERE id = $1
                """,
                session_id,
            )
    return counts
