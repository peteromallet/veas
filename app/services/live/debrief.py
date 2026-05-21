"""Agentic live debrief job — runs after a live conversation ends.

Reads the full transcript, applies partner/dyad privacy redaction, composes a
private non-chat bot turn, and gates on ``submit_live_debrief``.  Durable
writes (memories, observations, etc.) are allowed through the existing scoped
tool system and validated by the debrief safety gate (T5).

Per-session spend attribution (mediator.conversations.spend_usd_cents) is NOT
updated for debrief in Sprint 3 — that is deferred to Sprint 4.  Global text
LLM cost recording still flows through ``record_llm_cost`` via ``run_step``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.models.user import User

logger = logging.getLogger(__name__)

# ── Public API ───────────────────────────────────────────────────────────────


def _safe_str(val: Any) -> str:
    """Coerce a value to str, gracefully handling None / non-string types."""
    if val is None:
        return ""
    return str(val)


def _hash_text(text: str) -> str:
    """Return a SHA-256 hex digest of *text* for transcript policy."""
    return hashlib.sha256(text.encode()).hexdigest()


async def build_debrief_transcript_bundle(
    pool: Any,
    *,
    conversation_id: UUID,
    bot_id: str,
    user_id: UUID,
    partner_user_id: UUID | None,
) -> tuple[str, dict[str, Any]]:
    """Build the redacted transcript bundle and the server-side safety policy.

    Returns
    -------
    (model_bundle: str, transcript_policy: dict)
        *model_bundle* is the text block passed to the LLM as part of the
        debrief system task / hot context.
        *transcript_policy* is stored at ``ctx.extras['live_debrief_transcript_policy']``
        and consumed by the T5 safety gate (:func:`_debrief_write_guard_ok`).
    """
    # ── 1. Load transcript turns ────────────────────────────────────────
    turns = await pool.fetch(
        """\
        SELECT id, speaker_label, speaker_role, text, ts, active_item_id
        FROM mediator.transcript_turns
        WHERE conversation_id = $1
        ORDER BY ts
        """,
        conversation_id,
    )

    # ── 2. Load conversation speakers (consent state per label) ─────────
    speaker_rows = await pool.fetch(
        """\
        SELECT speaker_label, role, consent_state
        FROM mediator.conversation_speakers
        WHERE conversation_id = $1
        """,
        conversation_id,
    )
    consent_by_label: dict[str, str] = {
        row["speaker_label"]: row["consent_state"] for row in speaker_rows
    }

    # ── 3. Resolve dyad partner + per-bot partner_share ─────────────────
    partner_share: str | None = None
    if partner_user_id is not None:
        from app.services.partner_sharing import (
            get_partner_share,
            resolve_dyad_partner,
        )

        dyad = await resolve_dyad_partner(pool, user_id)
        if dyad is not None and dyad.partner_user_id == partner_user_id:
            partner_share = await get_partner_share(
                pool,
                user_id=partner_user_id,
                bot_id=bot_id,
            )

    # ── 4. Redaction pass ───────────────────────────────────────────────
    shareable_turn_ids: dict[str, dict[str, Any]] = {}
    redacted_turn_ids: list[str] = []
    model_lines: list[str] = []
    model_lines.append("=== LIVE SESSION TRANSCRIPT ===")
    model_lines.append("")

    for turn in turns:
        turn_id_str = str(turn["id"])
        role = turn["speaker_role"]
        label = turn["speaker_label"]
        text = turn["text"] or ""
        ts = turn["ts"]
        active_item_id = str(turn["active_item_id"]) if turn["active_item_id"] else None

        if role in ("primary", "bot"):
            # ── Always shareable ─────────────────────────────────────
            text_hash = _hash_text(text)
            shareable_turn_ids[turn_id_str] = {
                "text_hash": text_hash,
                "quote_hashes": [text_hash],
            }
            safe_label = label
            model_lines.append(
                f"[{role.upper()}] {safe_label} @ {ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)}"
            )
            if active_item_id:
                model_lines.append(f"  (active_item: {active_item_id})")
            model_lines.append(f"  {text}")
            model_lines.append("")

        elif role == "partner":
            # ── Shareable only with consent + opt-in ──────────────────
            consent = consent_by_label.get(label, "pending")
            if consent == "granted" and partner_share == "opt_in":
                text_hash = _hash_text(text)
                shareable_turn_ids[turn_id_str] = {
                    "text_hash": text_hash,
                    "quote_hashes": [text_hash],
                }
                model_lines.append(
                    f"[PARTNER] {label} @ {ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)}"
                )
                if active_item_id:
                    model_lines.append(f"  (active_item: {active_item_id})")
                model_lines.append(f"  {text}")
                model_lines.append("")
            else:
                # Redacted partner turn
                redacted_turn_ids.append(turn_id_str)
                reason = (
                    f"consent={consent}, partner_share={partner_share}"
                )
                model_lines.append(
                    f"[PARTNER — REDACTED: {reason}]"
                    f" @ {ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)}"
                )
                if active_item_id:
                    model_lines.append(f"  (active_item: {active_item_id})")
                model_lines.append("  [content redacted under partner-sharing policy]")
                model_lines.append("")

        else:
            # role == "other" — always redacted
            redacted_turn_ids.append(turn_id_str)
            model_lines.append(
                f"[OTHER — REDACTED] label={label}"
                f" @ {ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)}"
            )
            if active_item_id:
                model_lines.append(f"  (active_item: {active_item_id})")
            model_lines.append("  [content redacted — no sharing policy for 'other' speakers]")
            model_lines.append("")

    # ── 5. Build server-side transcript policy ──────────────────────────
    transcript_policy: dict[str, Any] = {
        "shareable_turn_ids": shareable_turn_ids,
        "redacted_turn_ids": redacted_turn_ids,
        "allow_hot_context_derived_writes": True,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "turn_count": len(turns),
        "shareable_count": len(shareable_turn_ids),
        "redacted_count": len(redacted_turn_ids),
    }

    model_bundle = "\n".join(model_lines)
    return model_bundle, transcript_policy


def _build_debrief_system_task(
    *,
    bot_spec: Any,
    bot_profile: dict[str, Any],
    prep_artifact: dict[str, Any] | None,
    agenda_items: list[dict[str, Any]],
    coverage_state: list[dict[str, Any]],
    transcript_bundle: str,
    conversation_notes: list[dict[str, Any]],
    tool_cap: int,
    hot_context_rendered: str,
) -> str:
    """Compose the debrief system task with all required instructions."""
    from app.services.live.bot_profile import format_live_bot_profile

    display_name = bot_profile.get("display_name") or bot_spec.bot_id
    topic_slug = bot_spec.primary_topic_slug or "general"

    lines: list[str] = []
    lines.append(
        f"You are {display_name}, performing a private post-session debrief "
        f"of a live voice conversation."
    )
    lines.append(f"Primary topic: {topic_slug}.")
    lines.append("This is a NON-CHAT turn — you are NOT talking to the user right now.")
    lines.append("")

    # ── Bot profile / persona ──────────────────────────────────────────
    lines.append("=== YOUR PERSONA ===")
    lines.append(format_live_bot_profile(bot_profile))
    lines.append("")

    # ── Hot context ─────────────────────────────────────────────────────
    if hot_context_rendered.strip():
        lines.append("=== HOT CONTEXT (user state before the session) ===")
        lines.append(hot_context_rendered)
        lines.append("")

    # ── Prep artifact ───────────────────────────────────────────────────
    if prep_artifact:
        lines.append("=== PREP BRIEF (agenda as planned before the session) ===")
        lines.append(json.dumps(prep_artifact, indent=2, default=str))
        lines.append("")

    # ── Agenda + coverage ───────────────────────────────────────────────
    if agenda_items:
        lines.append("=== AGENDA ITEMS ===")
        for item in agenda_items:
            lines.append(
                f"- {item.get('title', '(untitled)')} "
                f"[{item.get('status', 'unknown')}] "
                f"priority={item.get('priority', '?')}"
            )
        lines.append("")

    if coverage_state:
        lines.append("=== COVERAGE STATE (post-session) ===")
        for cs in coverage_state:
            lines.append(
                f"- item={cs.get('title', cs.get('item_id', '?'))} "
                f"status={cs.get('status', '?')} "
                f"coverage_summary={cs.get('coverage_summary', '')}"
            )
        lines.append("")

    # ── Conversation notes ──────────────────────────────────────────────
    if conversation_notes:
        lines.append("=== LIVE NOTES (captured during the session) ===")
        for note in conversation_notes:
            lines.append(f"- [{note.get('id', '?')}] {note.get('text', '')}")
        lines.append("")

    # ── Transcript ──────────────────────────────────────────────────────
    lines.append("=== FULL TRANSCRIPT (privacy-redacted) ===")
    lines.append(transcript_bundle)
    lines.append("")

    # ── Tool policy + constraints ───────────────────────────────────────
    lines.append("=== TOOL POLICY ===")
    lines.append(f"You may make up to {tool_cap} tool calls during this debrief.")
    lines.append("Outbound messaging tools are DISABLED — you cannot send messages.")
    lines.append("You have access to read tools, record/write tools (memories,")
    lines.append("observations, distillations, themes, watch items, commitments,")
    lines.append("events), and schedule tools.")
    lines.append("")

    # ── Evidence citation instructions ──────────────────────────────────
    lines.append("=== EVIDENCE CITATION RULES ===")
    lines.append("Every durable write (memory, observation, distillation, theme,")
    lines.append("commitment, event, watch item, or schedule) MUST cite its source.")
    lines.append("")
    lines.append("When citing transcript evidence, include in your tool call args:")
    lines.append('  "evidence_refs": [{"transcript_turn_id": "<uuid>",')
    lines.append('                         "quote": "<exact words from transcript>",')
    lines.append('                         "confidence": "high|medium|low"}]')
    lines.append("")
    lines.append("When deriving from hot context, bot notes, or the prep artifact")
    lines.append('instead of transcript, set "derivation_source" to one of:')
    lines.append('  "hot_context", "bot_notes", "prep_artifact"')
    lines.append("")
    lines.append("CRITICAL: Durable writes MUST NOT cite redacted transcript turns.")
    lines.append("The server will REJECT writes that reference redacted turns.")
    lines.append("Redacted turns are marked [REDACTED] in the transcript above —")
    lines.append("do not reference their turn IDs or quote their content.")
    lines.append("")

    # ── Required finalization ───────────────────────────────────────────
    lines.append("=== REQUIRED FINALIZATION ===")
    lines.append("When you have completed your analysis, you MUST call")
    lines.append("`submit_live_debrief` with your structured review.")
    lines.append("Plain text output (without calling submit_live_debrief) is a FAILED job.")
    lines.append("Exhausting your tool budget without calling submit_live_debrief")
    lines.append("is also a FAILED job.")
    lines.append("")
    lines.append("The submit_live_debrief payload must include:")
    lines.append("- review_summary: overall synthesis of the session")
    lines.append("- what_heard: key things the user said (with transcript evidence)")
    lines.append("- what_decided: decisions made or items covered")
    lines.append("- still_open: topics/items that remain unresolved")
    lines.append("- what_to_remember: durable facts to persist beyond the session")
    lines.append("- durable_write_summary: summary of durable writes you performed")
    lines.append("- open_questions: questions to flag for follow-up")
    lines.append("- references: list of evidence references with transcript_turn_id,")
    lines.append("  quote, and confidence")
    lines.append("- failed_writes: any writes that failed and why")

    return "\n".join(lines)


async def run_live_debrief_agentic_job(
    *,
    conversation_id: UUID,
    user: User,
    pool: Any,
) -> Any:
    """Run the agentic live-debrief turn for a conversation in status='debriefing'.

    Loads the conversations row, gathers all session artifacts, builds a
    partner-redacted transcript bundle, composes the debrief system task,
    and runs the non-chat agentic job with the LIVE_DEBRIEF_CONFIG.

    On success: persists ``conversation_artifacts(artifact_type='live_debrief')``
    and optionally ``review_summary``, then sets status to ``review_pending``.

    On failure: sets status to ``debrief_failed`` and stores failure details
    in ``session_fields``.

    Returns :class:`NonchatJobResult`.
    """
    # ── Function-scoped imports to avoid circular deps ──────────────────
    from app.bots.registry import get_bot_spec
    from app.services.live import artifacts as live_artifacts
    from app.services.hot_context import build_hot_context, render_hot_context
    from app.services.hot_context_solo import (
        build_hot_context_solo,
        render_hot_context_solo,
    )
    from app.services.live.bot_profile import (
        format_live_bot_profile,
        live_bot_profile_context,
        user_from_live_row,
    )
    from app.services.nonchat_agentic import (
        LIVE_DEBRIEF_CONFIG,
        NonchatJobConfig,
        NonchatJobResult,
        run_agentic_nonchat_job,
    )
    from app.services.tools.registry import build_live_debrief_tools

    settings = get_settings()

    # ── 1. Load the conversations row ───────────────────────────────────
    row = await pool.fetchrow(
        """\
        SELECT id, user_id, partner_user_id, bot_id, mode, steering_text,
               status, topic_id, session_fields, prep_summary, current_item_id,
               started_at, ended_at
        FROM mediator.conversations
        WHERE id = $1
        """,
        conversation_id,
    )
    if row is None:
        raise ValueError(
            f"conversation_id={conversation_id} not found in mediator.conversations"
        )
    if row["status"] != "debriefing":
        raise ValueError(
            f"conversation_id={conversation_id} has status={row['status']!r}, "
            f"expected 'debriefing'"
        )

    bot_id: str = row["bot_id"]
    user_id: UUID = row["user_id"]
    partner_user_id: UUID | None = row["partner_user_id"]
    resolved_topic_id: UUID | None = row["topic_id"]

    # ── 2. Load partner user if present ─────────────────────────────────
    partner: User | None = None
    if partner_user_id is not None:
        partner_row = await pool.fetchrow(
            "SELECT * FROM users WHERE id = $1", partner_user_id
        )
        if partner_row is not None:
            partner = user_from_live_row(partner_user_id, partner_row)

    # ── 3. Resolve bot spec ─────────────────────────────────────────────
    try:
        bot_spec = get_bot_spec(bot_id)
    except Exception:
        logger.warning(
            "live_debrief: unknown bot_id=%s — cannot resolve bot spec",
            bot_id,
        )
        try:
            await _set_debrief_failed(
                pool, conversation_id, "unknown bot_id", turn_id=None
            )
        except Exception:
            logger.exception(
                "live_debrief: _set_debrief_failed itself raised conversation_id=%s",
                conversation_id,
            )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="unknown bot_id",
            turn_id=None,
            tool_call_count=0,
        )

    # ── 4. Build bot profile ────────────────────────────────────────────
    bot_profile = live_bot_profile_context(
        bot_id, user=user, partner=partner,
    )

    # ── 5. Load prep artifact ───────────────────────────────────────────
    prep_artifact: dict[str, Any] | None = None
    try:
        prep_row = await live_artifacts.get_current_artifact(
            pool,
            conversation_id=str(conversation_id),
            artifact_type="live_prep_brief",
        )
        if prep_row is not None:
            prep_artifact = prep_row.payload
    except Exception:
        logger.debug("live_debrief: no prep artifact found for %s", conversation_id)

    # ── 6. Load agenda items + coverage state ───────────────────────────
    agenda_items: list[dict[str, Any]] = []
    coverage_state: list[dict[str, Any]] = []
    try:
        item_rows = await pool.fetch(
            """\
            SELECT id, title, status, priority, kind, intent, ask,
                   done_when, coverage_summary, coverage_evidence_quote,
                   speaker_scope, order_hint
            FROM mediator.conversation_items
            WHERE conversation_id = $1
            ORDER BY order_hint, created_at
            """,
            conversation_id,
        )
        for ir in item_rows:
            item = {
                "id": str(ir["id"]),
                "title": ir["title"],
                "status": ir["status"],
                "priority": ir["priority"],
                "kind": ir["kind"],
                "intent": ir["intent"],
                "ask": ir["ask"],
                "done_when": ir["done_when"],
                "speaker_scope": ir["speaker_scope"],
            }
            agenda_items.append(item)
            if ir.get("coverage_summary") or ir["status"] == "covered":
                coverage_state.append({
                    "item_id": str(ir["id"]),
                    "title": ir["title"],
                    "status": ir["status"],
                    "coverage_summary": ir.get("coverage_summary") or "",
                    "coverage_evidence_quote": ir.get("coverage_evidence_quote") or "",
                })
    except Exception:
        logger.warning(
            "live_debrief: failed to load agenda items for %s", conversation_id,
            exc_info=True,
        )

    # ── 7. Load conversation notes ──────────────────────────────────────
    conversation_notes: list[dict[str, Any]] = []
    try:
        note_rows = await pool.fetch(
            """\
            SELECT id, text, attributed_to_speaker, evidence_turn_id, created_at
            FROM mediator.conversation_notes
            WHERE conversation_id = $1
            ORDER BY created_at
            """,
            conversation_id,
        )
        conversation_notes = [
            {
                "id": str(nr["id"]),
                "text": nr["text"],
                "attributed_to_speaker": nr["attributed_to_speaker"],
                "evidence_turn_id": (
                    str(nr["evidence_turn_id"]) if nr["evidence_turn_id"] else None
                ),
                "created_at": (
                    nr["created_at"].isoformat()
                    if hasattr(nr["created_at"], "isoformat")
                    else str(nr["created_at"])
                ),
            }
            for nr in note_rows
        ]
    except Exception:
        logger.warning(
            "live_debrief: failed to load notes for %s", conversation_id,
            exc_info=True,
        )

    # ── 8. Build transcript bundle with partner redaction ───────────────
    transcript_bundle, transcript_policy = await build_debrief_transcript_bundle(
        pool,
        conversation_id=conversation_id,
        bot_id=bot_id,
        user_id=user_id,
        partner_user_id=partner_user_id,
    )

    # ── 9. Build normal hot context string ──────────────────────────────
    try:
        allow_cross_topic_peek = getattr(
            getattr(bot_spec, "read_scopes", None),
            "allow_cross_topic_peek",
            False,
        )
        if getattr(bot_spec, "participants_shape", None) == "solo" or partner is None:
            if resolved_topic_id is None:
                raise ValueError("live_debrief hot context requires topic_id")
            hot_context = await build_hot_context_solo(
                pool,
                user,
                [],
                trigger_metadata={"kind": "live_debrief", "conversation_id": str(conversation_id)},
                primary_topic_id=resolved_topic_id,
                bot_id=bot_id,
                allow_cross_topic_peek=allow_cross_topic_peek,
            )
            hot_context_rendered = render_hot_context_solo(hot_context)
        else:
            hot_context = await build_hot_context(
                pool,
                user,
                partner,
                [],
                trigger_metadata={"kind": "live_debrief", "conversation_id": str(conversation_id)},
                primary_topic_id=resolved_topic_id,
                allow_cross_topic_peek=allow_cross_topic_peek,
                allow_cross_topic_status_injection=getattr(
                    getattr(bot_spec, "read_scopes", None),
                    "allow_cross_topic_status_injection",
                    False,
                ),
                bot_id=bot_id,
            )
            hot_context_rendered = render_hot_context(hot_context)
    except Exception:
        logger.warning(
            "live_debrief: falling back to live bot profile context for %s",
            conversation_id,
            exc_info=True,
        )
        hot_context_rendered = format_live_bot_profile(bot_profile)

    # ── 10. Build debrief tool caps ─────────────────────────────────────
    tool_cap = settings.live_debrief_tool_call_cap
    flat_allowed_tools = build_live_debrief_tools(bot_spec)

    # ── 11. Compose system task ─────────────────────────────────────────
    system_task = _build_debrief_system_task(
        bot_spec=bot_spec,
        bot_profile=bot_profile,
        prep_artifact=prep_artifact,
        agenda_items=agenda_items,
        coverage_state=coverage_state,
        transcript_bundle=transcript_bundle,
        conversation_notes=conversation_notes,
        tool_cap=tool_cap,
        hot_context_rendered=hot_context_rendered,
    )

    # ── 12. Build trigger_metadata ──────────────────────────────────────
    trigger_meta: dict[str, Any] = {
        "kind": "live_debrief",
        "conversation_id": str(conversation_id),
        "bot_id": bot_id,
    }

    # ── 13. Configure the debrief job ───────────────────────────────────
    debrief_config = NonchatJobConfig(
        current_step=LIVE_DEBRIEF_CONFIG.current_step,
        submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
        submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
        allowed_tools=flat_allowed_tools,
        failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        max_tool_calls=tool_cap,
        initial_extras={
            "live_debrief_transcript_policy": transcript_policy,
        },
    )

    # ── 14. Run the non-chat agentic job ────────────────────────────────
    result = await run_agentic_nonchat_job(
        kind="live_debrief",
        user=user,
        conversation_id=conversation_id,
        system_task=system_task,
        pool=pool,
        bot_spec=bot_spec,
        bot_id=bot_id,
        topic_id=resolved_topic_id,
        partner=partner,
        hot_context=hot_context_rendered,
        trigger_metadata=trigger_meta,
        config=debrief_config,
    )

    # ── 15. Resolve provisional artifact ID for failure-path cleanup ────
    _provisional_artifact_id: str | None = None
    if hasattr(result, "extras") and isinstance(result.extras, dict):
        _provisional_artifact_id = result.extras.get("_provisional_artifact_id")

    # ── 16. Persist outcome ─────────────────────────────────────────────
    if result.success and result.brief:
        try:
            await _persist_debrief_success(
                pool,
                conversation_id,
                user_id,
                bot_id,
                result,
            )
        except Exception as exc:
            logger.exception(
                "live_debrief: artifact persistence failed conversation_id=%s",
                conversation_id,
            )
            await _set_debrief_failed(
                pool,
                conversation_id,
                "live_debrief_persistence_failed",
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
                error=str(exc),
                artifact_id=_provisional_artifact_id,
            )
            # Return a failed result so the caller knows persistence broke.
            return NonchatJobResult(
                success=False,
                brief=result.brief,
                failure_reason="live_debrief_persistence_failed",
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
            )
    else:
        failure_reason = result.failure_reason or "live_debrief_submit_missing"
        try:
            await _set_debrief_failed(
                pool,
                conversation_id,
                failure_reason,
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
                artifact_id=_provisional_artifact_id,
            )
        except Exception:
            logger.exception(
                "live_debrief: _set_debrief_failed itself raised conversation_id=%s",
                conversation_id,
            )

    return result


async def retry_live_debrief(
    conversation_id: UUID,
    pool: Any,
) -> Any:
    """Retry a failed live debrief session.

    Checks that the conversation is in ``debrief_failed`` status, resets it
    to ``debriefing``, and re-runs ``run_live_debrief_agentic_job``.
    """
    row = await pool.fetchrow(
        "SELECT id, user_id, bot_id, topic_id, status "
        "FROM mediator.conversations WHERE id = $1",
        conversation_id,
    )
    if row is None:
        raise ValueError(
            f"retry_live_debrief: conversation_id={conversation_id} not found"
        )
    if row["status"] != "debrief_failed":
        raise ValueError(
            f"retry_live_debrief: conversation_id={conversation_id} "
            f"has status={row['status']!r}, expected 'debrief_failed'"
        )

    # Reset to debriefing
    await pool.execute(
        "UPDATE mediator.conversations SET status = 'debriefing' WHERE id = $1",
        conversation_id,
    )

    # Load user record
    user_row = await pool.fetchrow(
        "SELECT * FROM users WHERE id = $1", row["user_id"]
    )
    if user_row is None:
        raise ValueError(f"user_id={row['user_id']} not found in users")

    from app.services.live.bot_profile import user_from_live_row
    user = user_from_live_row(row["user_id"], user_row)

    return await run_live_debrief_agentic_job(
        conversation_id=conversation_id,
        user=user,
        pool=pool,
    )


# ── Internal helpers ────────────────────────────────────────────────────────


async def _persist_debrief_success(
    pool: Any,
    conversation_id: UUID,
    user_id: UUID,
    bot_id: str,
    result: Any,
) -> None:
    """Persist the submitted debrief payload by finalizing the provisional artifact.

    Sprint 4: Instead of creating a new post-hoc artifact (which would be
    disconnected from the provenance links accumulated during the debrief
    turn), this helper **finalizes** the provisional artifact that was
    created before the first guarded durable write.  All artifact_links
    rows already point to this artifact revision — finalizing it keeps
    that relationship intact.

    Falls back to creating a new artifact when no provisional artifact
    exists (backward compatibility with pre-Sprint-4 or error paths).
    """
    from app.services.live import artifacts as live_artifacts
    from app.services.live.provenance import finalize_live_debrief_artifact

    brief = result.brief or {}
    conv_id_str = str(conversation_id)
    turn_id_str = str(result.turn_id) if result.turn_id else None

    # ── Resolve the provisional artifact ID from the job result ──────
    provisional_artifact_id: str | None = None
    if hasattr(result, "extras") and isinstance(result.extras, dict):
        provisional_artifact_id = result.extras.get("_provisional_artifact_id")

    async with pool.acquire() as conn:
        async with conn.transaction():
            if provisional_artifact_id:
                # ── Sprint 4: finalize the provisional artifact ──────
                # This updates the same artifact row that all durable-write
                # links reference — no disconnected post-hoc artifact.
                finalized = await finalize_live_debrief_artifact(
                    conn,
                    artifact_id=provisional_artifact_id,
                    content=brief,
                    created_by_turn_id=turn_id_str or "",
                )
                logger.info(
                    "live_debrief: finalized provisional artifact_id=%s "
                    "conversation_id=%s",
                    finalized.id,
                    conversation_id,
                )
            else:
                # ── Fallback: no provisional artifact (pre-Sprint-4 or error) ──
                logger.warning(
                    "live_debrief: no provisional artifact in result.extras — "
                    "creating post-hoc artifact (links will be disconnected) "
                    "conversation_id=%s",
                    conversation_id,
                )
                await live_artifacts.create_artifact(
                    conn,
                    conversation_id=conv_id_str,
                    bot_id=bot_id,
                    user_id=str(user_id),
                    artifact_type="live_debrief",
                    payload=brief,
                    payload_version=1,
                    created_by_turn_id=turn_id_str,
                )

            # If review_summary is present, create a separate artifact
            review_summary = brief.get("review_summary")
            if review_summary and isinstance(review_summary, str) and review_summary.strip():
                await live_artifacts.create_artifact(
                    conn,
                    conversation_id=conv_id_str,
                    bot_id=bot_id,
                    user_id=str(user_id),
                    artifact_type="review_summary",
                    payload={"review_summary": review_summary},
                    payload_version=1,
                    created_by_turn_id=turn_id_str,
                )

            # Update conversation status to review_pending
            await conn.execute(
                """\
                UPDATE mediator.conversations
                SET status = 'review_pending',
                    ended_at = COALESCE(ended_at, now())
                WHERE id = $1
                """,
                conversation_id,
            )

    logger.info(
        "live_debrief: success conversation_id=%s turn_id=%s",
        conversation_id,
        result.turn_id,
    )


async def _set_debrief_failed(
    pool: Any,
    conversation_id: UUID,
    failure_reason: str,
    *,
    turn_id: UUID | None = None,
    tool_call_count: int = 0,
    error: str | None = None,
    artifact_id: str | None = None,
) -> None:
    """Mark a conversation as debrief_failed and store failure details.

    Sprint 4: When *artifact_id* is provided (the provisional provenance
    artifact), soft-deletes the artifact and its links via
    :func:`mark_live_debrief_artifact_failed` so reverse provenance
    remains inspectable even after failure.

    Parameters
    ----------
    error:
        Optional exception / traceback string for the ``debrief_error``
        field.  Set when a persistence operation itself raises.
    artifact_id:
        Optional UUID string of the provisional live_debrief artifact.
        When set, soft-deletes the artifact and all of its artifact_links
        rows, keeping the rows discoverable via ``include_deleted=True``
        on reverse-provenance queries.
    """
    # ── Sprint 4: soft-delete provisional artifact + links ──────────────
    if artifact_id:
        try:
            from app.services.live.provenance import (
                mark_live_debrief_artifact_failed,
            )

            async with pool.acquire() as conn:
                async with conn.transaction():
                    await mark_live_debrief_artifact_failed(
                        conn,
                        artifact_id=artifact_id,
                        reason=failure_reason,
                    )
        except Exception:
            logger.exception(
                "live_debrief: mark_live_debrief_artifact_failed itself "
                "raised conversation_id=%s artifact_id=%s — "
                "continuing to set conversation status",
                conversation_id,
                artifact_id,
            )

    failure_details = {
        "debrief_failure_reason": failure_reason,
        "debrief_error": error,
        "debrief_turn_id": str(turn_id) if turn_id else None,
        "debrief_tool_call_count": tool_call_count,
        "debrief_failed_at": datetime.now(timezone.utc).isoformat(),
    }

    await pool.execute(
        """\
        UPDATE mediator.conversations
        SET status = 'debrief_failed',
            session_fields = COALESCE(session_fields, '{}'::jsonb)
                             || $2::jsonb
        WHERE id = $1
        """,
        conversation_id,
        json.dumps(failure_details),
    )

    logger.warning(
        "live_debrief: failed conversation_id=%s reason=%s turn_id=%s",
        conversation_id,
        failure_reason,
        turn_id,
    )
