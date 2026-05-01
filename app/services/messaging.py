"""Outbound messaging helper with provider-specific delivery rules."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.models.user import User
from app.config import get_settings
from app.services import discord, hooks, system_state, whatsapp
from app.services.crypto import encrypt_value
from app.services.templates import TemplateCall, render_template
from app.services.withheld_reviews import record_withheld_outbound_review

logger = logging.getLogger(__name__)

_RETRY_BACKOFF_SECONDS = [1, 2, 4]


async def _append_turn_reasoning(pool: Any, bot_turn_id: UUID | None, note: str) -> None:
    if bot_turn_id is None:
        return
    await pool.execute(
        "UPDATE bot_turns SET reasoning = COALESCE(reasoning, '') || $1 WHERE id = $2",
        f"\n{note}",
        bot_turn_id,
    )


async def _insert_outbound(pool: Any, user: User, content: str, state: str = "raw") -> UUID:
    row = await pool.fetchrow(
        """
        INSERT INTO messages (direction, recipient_id, content, content_encrypted, processing_state, sent_at)
        VALUES ('outbound', $1, $2, $3, $4, now())
        RETURNING id
        """,
        user.id,
        content,
        encrypt_value(content),
        state,
    )
    return row["id"]


async def _send_with_retry(send_call) -> dict[str, Any]:
    last_error: Exception | None = None
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            return await send_call()
        except Exception as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                break
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    raise last_error  # type: ignore[misc]


async def _call_oob_hook(
    pool: Any,
    content: str,
    recipient_id: UUID,
    protected_owner_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    hook = hooks.check_oob
    if hook is None:
        return {"verdict": "ok", "reason": "OOB hook disabled", "suggested_rewrite": None, "checker_failed": False}
    try:
        verdict = await hook(pool, content, recipient_id, protected_owner_ids=protected_owner_ids)
    except TypeError:
        try:
            verdict = await hook(pool, content, recipient_id)
        except TypeError:
            verdict = await hook(content, recipient_id)
    if hasattr(verdict, "model_dump"):
        verdict = verdict.model_dump(mode="json")
    if "suggested_rewrite" not in verdict and "rewrite" in verdict:
        verdict["suggested_rewrite"] = verdict.get("rewrite")
    verdict.setdefault("checker_failed", False)
    verdict.setdefault("reason", "")
    return verdict


async def send_outbound(
    pool: Any,
    user: User,
    content: str,
    *,
    template_fallback: TemplateCall | None = None,
    bot_turn_id: UUID | None = None,
    ignore_pause: bool = False,
    protected_owner_ids: list[UUID] | None = None,
) -> UUID:
    if not ignore_pause and (await system_state.is_paused(pool) or await hooks.paused_for_user(user.id)):
        return await _insert_outbound(pool, user, content, "withheld")

    verdict = await _call_oob_hook(pool, content, user.id, protected_owner_ids)
    if verdict["verdict"] == "block":
        await _append_turn_reasoning(pool, bot_turn_id, f"Outbound blocked by OOB hook: {verdict['reason']}")
        row_id = await _insert_outbound(pool, user, content, "withheld")
        await record_withheld_outbound_review(
            pool,
            recipient_id=user.id,
            outbound_id=row_id,
            original_content=content,
            suggested_rewrite=verdict.get("suggested_rewrite"),
            reason=verdict["reason"],
            verdict="block",
            checker_failed=bool(verdict.get("checker_failed")),
        )
        return row_id
    if verdict["verdict"] == "rewrite":
        await _append_turn_reasoning(pool, bot_turn_id, f"Outbound withheld for OOB rewrite review: {verdict['reason']}")
        row_id = await _insert_outbound(pool, user, content, "withheld")
        await record_withheld_outbound_review(
            pool,
            recipient_id=user.id,
            outbound_id=row_id,
            original_content=content,
            suggested_rewrite=verdict.get("suggested_rewrite"),
            reason=verdict["reason"],
            verdict="rewrite",
            checker_failed=bool(verdict.get("checker_failed")),
        )
        return row_id
    if verdict.get("checker_failed"):
        logger.warning("OOB checker failed open for recipient_id=%s: %s", user.id, verdict.get("reason"))

    provider = get_settings().messaging_provider.strip().lower()
    if provider == "discord":
        within_window = True
    else:
        last_inbound_at = await pool.fetchval(
            "SELECT MAX(sent_at) FROM messages WHERE sender_id=$1 AND direction='inbound'",
            user.id,
        )
        within_window = last_inbound_at is not None and datetime.now(UTC) - last_inbound_at < timedelta(hours=24)

    if not within_window and template_fallback is None:
        await _append_turn_reasoning(pool, bot_turn_id, "Outbound deferred: outside WhatsApp 24h window with no template")
        return await _insert_outbound(pool, user, content, "withheld")

    template_payload = None
    if not within_window:
        template_payload = render_template(template_fallback)

    row_id = await _insert_outbound(pool, user, content)

    async def send_call() -> dict[str, Any]:
        if provider == "discord":
            return await discord.send_text(user.phone, content)
        if within_window:
            return await whatsapp.send_text(user.phone, content)
        return await whatsapp.send_template(user.phone, template_payload)

    try:
        response = await _send_with_retry(send_call)
    except Exception as exc:
        logger.warning("outbound send failed after retries: %s", exc)
        await pool.execute("UPDATE messages SET processing_state='expired' WHERE id=$1", row_id)
        await _append_turn_reasoning(pool, bot_turn_id, f"Outbound send failed: {exc}")
        return row_id

    wa_id = response["messages"][0]["id"]
    await pool.execute(
        "UPDATE messages SET whatsapp_message_id=$1, processing_state='processed' WHERE id=$2",
        wa_id,
        row_id,
    )
    return row_id
