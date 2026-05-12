"""Outbound messaging helper with provider-specific delivery rules."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.models.user import User, claim_onboarding_welcome
from app.config import get_settings
from app.services import discord, hooks, system_state, whatsapp
from app.services.crypto import encrypt_value
from app.services.templates import TemplateCall, render_template
from app.services.withheld_reviews import record_withheld_outbound_review

logger = logging.getLogger(__name__)

_RETRY_BACKOFF_SECONDS = [1, 2, 4]


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return row.get(key, default) if hasattr(row, "get") else default


async def _append_turn_reasoning(pool: Any, bot_turn_id: UUID | None, note: str) -> None:
    if bot_turn_id is None:
        return
    existing = await pool.fetchval("SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id=$1", bot_turn_id)
    updated = f"{existing or ''}\n{note}"
    await pool.execute(
        "UPDATE bot_turns SET reasoning=$1, reasoning_encrypted=$2 WHERE id=$3",
        updated,
        encrypt_value(updated),
        bot_turn_id,
    )


async def _insert_outbound(
    pool: Any,
    user: User,
    content: str,
    state: str = "raw",
    *,
    bot_turn_id: UUID | None = None,
    outbound_part_key: str | None = None,
    outbound_part_index: int | None = None,
    bot_id: str | None = None,
    topic_id: UUID | None = None,
) -> UUID:
    if bot_turn_id is not None or outbound_part_key is not None or outbound_part_index is not None:
        row = await pool.fetchrow(
            """
            INSERT INTO messages (
                direction, recipient_id, content, content_encrypted, processing_state, sent_at,
                bot_turn_id, outbound_part_key, outbound_part_index, bot_id, topic_id
            )
            VALUES ('outbound', $1, $2, $3, $4, now(), $5, $6, $7, $8, $9)
            ON CONFLICT (outbound_part_key) DO NOTHING
            RETURNING id
            """,
            user.id,
            content,
            encrypt_value(content),
            state,
            bot_turn_id,
            outbound_part_key,
            outbound_part_index,
            bot_id,
            topic_id,
        )
        if row is not None:
            return row["id"]
        if outbound_part_key is None:
            raise RuntimeError("outbound insert unexpectedly conflicted without a part key")
        existing = await pool.fetchrow(
            "SELECT id FROM messages WHERE outbound_part_key=$1",
            outbound_part_key,
        )
        if existing is None:
            raise RuntimeError("outbound part conflict without an existing row")
        return existing["id"]
    row = await pool.fetchrow(
        """
        INSERT INTO messages (direction, recipient_id, content, content_encrypted, processing_state, sent_at, bot_id, topic_id)
        VALUES ('outbound', $1, $2, $3, $4, now(), $5, $6)
        RETURNING id
        """,
        user.id,
        content,
        encrypt_value(content),
        state,
        bot_id,
        topic_id,
    )
    return row["id"]


async def _fetch_outbound_part(pool: Any, part_key: str) -> dict[str, Any] | None:
    return await pool.fetchrow(
        """
        SELECT id, processing_state, whatsapp_message_id, content
        FROM messages
        WHERE outbound_part_key=$1
        """,
        part_key,
    )


async def sent_contents_for_turn(pool: Any, turn_id: UUID) -> list[str]:
    rows = await pool.fetch(
        """
        SELECT content
        FROM messages
        WHERE bot_turn_id=$1
          AND direction='outbound'
          AND processing_state='processed'
          AND outbound_part_index IS NOT NULL
        ORDER BY outbound_part_index ASC, sent_at ASC
        """,
        turn_id,
    )
    return [_row_get(row, "content") for row in rows if _row_get(row, "content")]


async def send_outbound_part(
    pool: Any,
    user: User,
    content: str,
    *,
    bot_turn_id: UUID,
    part_key: str,
    part_index: int,
    client_part_key: str | None = None,
    protected_owner_ids: list[UUID] | None = None,
    send_typing_indicator: bool = True,
    before_provider_send: Callable[[], Awaitable[None]] | None = None,
    bot_id: str | None = None,
    topic_id: UUID | None = None,
) -> dict[str, Any]:
    existing = await _fetch_outbound_part(pool, part_key)
    if (
        existing is not None
        and _row_get(existing, "processing_state") == "processed"
        and _row_get(existing, "whatsapp_message_id")
    ):
        return {
            "status": "duplicate",
            "part_key": part_key,
            "client_part_key": client_part_key,
            "message_id": existing["id"],
            "provider_message_id": _row_get(existing, "whatsapp_message_id"),
            "delivered_content": _row_get(existing, "content"),
            "visible_to_user": True,
            "sent_so_far": await sent_contents_for_turn(pool, bot_turn_id),
            "reason": "already sent for this runtime idempotency key",
            "suggested_rewrite": None,
        }

    provider = get_settings().messaging_provider.strip().lower()
    if provider != "discord":
        return {
            "status": "not_enabled",
            "part_key": part_key,
            "client_part_key": client_part_key,
            "message_id": None,
            "provider_message_id": None,
            "delivered_content": None,
            "visible_to_user": False,
            "sent_so_far": await sent_contents_for_turn(pool, bot_turn_id),
            "reason": f"incremental message parts are not enabled for provider {provider}",
            "suggested_rewrite": None,
        }

    if await system_state.is_paused(pool) or await hooks.paused_for_user(user.id) or (bot_id and await system_state.user_bot_paused(pool, user.id, bot_id)):
        row_id = await _insert_outbound(
            pool,
            user,
            content,
            "withheld",
            bot_turn_id=bot_turn_id,
            outbound_part_key=part_key,
            outbound_part_index=part_index,
            bot_id=bot_id,
            topic_id=topic_id,
        )
        return {
            "status": "withheld",
            "part_key": part_key,
            "client_part_key": client_part_key,
            "message_id": row_id,
            "provider_message_id": None,
            "delivered_content": None,
            "visible_to_user": False,
            "sent_so_far": await sent_contents_for_turn(pool, bot_turn_id),
            "reason": "sending is paused",
            "suggested_rewrite": None,
        }

    verdict = await _call_oob_hook(pool, content, user.id, protected_owner_ids)
    if verdict["verdict"] in {"block", "rewrite"}:
        reason = verdict["reason"]
        status = "blocked" if verdict["verdict"] == "block" else "withheld"
        await _append_turn_reasoning(pool, bot_turn_id, f"Incremental outbound {status} by OOB hook: {reason}")
        row_id = await _insert_outbound(
            pool,
            user,
            content,
            "withheld",
            bot_turn_id=bot_turn_id,
            outbound_part_key=part_key,
            outbound_part_index=part_index,
            bot_id=bot_id,
            topic_id=topic_id,
        )
        await record_withheld_outbound_review(
            pool,
            recipient_id=user.id,
            outbound_id=row_id,
            original_content=content,
            suggested_rewrite=verdict.get("suggested_rewrite"),
            reason=reason,
            verdict=verdict["verdict"],
            checker_failed=bool(verdict.get("checker_failed")),
            bot_id=bot_id,
            topic_id=topic_id,
        )
        return {
            "status": status,
            "part_key": part_key,
            "client_part_key": client_part_key,
            "message_id": row_id,
            "provider_message_id": None,
            "delivered_content": None,
            "visible_to_user": False,
            "sent_so_far": await sent_contents_for_turn(pool, bot_turn_id),
            "reason": reason,
            "suggested_rewrite": verdict.get("suggested_rewrite"),
        }

    if before_provider_send is not None:
        await before_provider_send()

    row_id = await _insert_outbound(
        pool,
        user,
        content,
        bot_turn_id=bot_turn_id,
        outbound_part_key=part_key,
        outbound_part_index=part_index,
        bot_id=bot_id,
        topic_id=topic_id,
    )

    try:
        response = await _send_with_retry(
            lambda: discord.send_text(user.phone, content, send_typing_indicator=send_typing_indicator)
        )
    except Exception as exc:
        logger.warning("incremental outbound send failed after retries: %s", exc,
                       extra={"bot_id": bot_id or "mediator", "topic_id": str(topic_id) if topic_id else None})
        await pool.execute("UPDATE messages SET processing_state='expired' WHERE id=$1", row_id)
        await _append_turn_reasoning(pool, bot_turn_id, f"Incremental outbound send failed: {exc}")
        return {
            "status": "provider_failed",
            "part_key": part_key,
            "client_part_key": client_part_key,
            "message_id": row_id,
            "provider_message_id": None,
            "delivered_content": None,
            "visible_to_user": False,
            "sent_so_far": await sent_contents_for_turn(pool, bot_turn_id),
            "reason": str(exc),
            "suggested_rewrite": None,
        }

    provider_message_id = response["messages"][0]["id"]
    await pool.execute(
        "UPDATE messages SET whatsapp_message_id=$1, processing_state='processed' WHERE id=$2",
        provider_message_id,
        row_id,
    )
    await claim_onboarding_welcome(pool, user.id)
    return {
        "status": "sent",
        "part_key": part_key,
        "client_part_key": client_part_key,
        "message_id": row_id,
        "provider_message_id": provider_message_id,
        "delivered_content": content,
        "visible_to_user": True,
        "sent_so_far": await sent_contents_for_turn(pool, bot_turn_id),
        "reason": None,
        "suggested_rewrite": None,
    }


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
    send_typing_indicator: bool = True,
    before_provider_send: Callable[[], Awaitable[None]] | None = None,
    bot_id: str | None = None,
    topic_id: UUID | None = None,
) -> UUID:
    if not ignore_pause and (await system_state.is_paused(pool) or await hooks.paused_for_user(user.id) or (bot_id and await system_state.user_bot_paused(pool, user.id, bot_id))):
        return await _insert_outbound(pool, user, content, "withheld", bot_turn_id=bot_turn_id, bot_id=bot_id, topic_id=topic_id)

    verdict = await _call_oob_hook(pool, content, user.id, protected_owner_ids)
    if verdict["verdict"] == "block":
        await _append_turn_reasoning(pool, bot_turn_id, f"Outbound blocked by OOB hook: {verdict['reason']}")
        row_id = await _insert_outbound(pool, user, content, "withheld", bot_turn_id=bot_turn_id, bot_id=bot_id, topic_id=topic_id)
        await record_withheld_outbound_review(
            pool,
            recipient_id=user.id,
            outbound_id=row_id,
            original_content=content,
            suggested_rewrite=verdict.get("suggested_rewrite"),
            reason=verdict["reason"],
            verdict="block",
            checker_failed=bool(verdict.get("checker_failed")),
            bot_id=bot_id,
            topic_id=topic_id,
        )
        return row_id
    if verdict["verdict"] == "rewrite":
        await _append_turn_reasoning(pool, bot_turn_id, f"Outbound withheld for OOB rewrite review: {verdict['reason']}")
        row_id = await _insert_outbound(pool, user, content, "withheld", bot_turn_id=bot_turn_id, bot_id=bot_id, topic_id=topic_id)
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
        logger.warning("OOB checker failed open for recipient_id=%s: %s", user.id, verdict.get("reason"),
                       extra={"bot_id": bot_id or "mediator", "topic_id": str(topic_id) if topic_id else None})

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
        return await _insert_outbound(pool, user, content, "withheld", bot_turn_id=bot_turn_id, bot_id=bot_id, topic_id=topic_id)

    template_payload = None
    if not within_window:
        template_payload = render_template(template_fallback)

    if before_provider_send is not None:
        await before_provider_send()

    row_id = await _insert_outbound(pool, user, content, bot_turn_id=bot_turn_id, bot_id=bot_id, topic_id=topic_id)

    async def send_call() -> dict[str, Any]:
        if provider == "discord":
            return await discord.send_text(user.phone, content, send_typing_indicator=send_typing_indicator)
        if within_window:
            return await whatsapp.send_text(user.phone, content)
        return await whatsapp.send_template(user.phone, template_payload)

    try:
        response = await _send_with_retry(send_call)
    except Exception as exc:
        logger.warning("outbound send failed after retries: %s", exc,
                       extra={"bot_id": bot_id or "mediator", "topic_id": str(topic_id) if topic_id else None})
        await pool.execute("UPDATE messages SET processing_state='expired' WHERE id=$1", row_id)
        await _append_turn_reasoning(pool, bot_turn_id, f"Outbound send failed: {exc}")
        return row_id

    wa_id = response["messages"][0]["id"]
    await pool.execute(
        "UPDATE messages SET whatsapp_message_id=$1, processing_state='processed' WHERE id=$2",
        wa_id,
        row_id,
    )
    await claim_onboarding_welcome(pool, user.id)
    return row_id
