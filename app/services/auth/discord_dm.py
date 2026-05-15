"""Send a Discord DM via the mediator bot's REST token.

Uses the existing ``DISCORD_BOT_TOKEN_<bot_id>`` (or ``DISCORD_BOT_TOKEN``
fallback) from env. In environments where no token is present, the
function logs the would-be DM at INFO level and returns
``DmResult(dispatched=False, dm_channel_id=None)`` so dev runs work
without a Discord app.

This module is intentionally thin — it only does the open-DM + send-text
two-step. Anything richer belongs in ``app/services/discord.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"


@dataclass(frozen=True)
class DmResult:
    dispatched: bool
    dm_channel_id: str | None
    reason: str | None = None


def _bot_token(bot_id: str = "mediator") -> str | None:
    raw = os.environ.get(f"DISCORD_BOT_TOKEN_{bot_id.upper()}")
    if not raw:
        raw = os.environ.get("DISCORD_BOT_TOKEN")
    return (raw or "").strip() or None


async def send_dm(discord_user_id: str, content: str, *, bot_id: str = "mediator") -> DmResult:
    """Open a DM channel with ``discord_user_id`` and send ``content``.

    Returns a :class:`DmResult` describing whether the send happened.
    Errors are swallowed and surfaced as ``DmResult(dispatched=False, reason=…)``
    — the caller decides whether to fail the auth flow on a DM failure.
    """
    token = _bot_token(bot_id)
    if token is None:
        logger.info(
            "discord_dm: no bot token configured; would have DM'd discord_id=%s content=%r",
            discord_user_id,
            content,
        )
        return DmResult(dispatched=False, dm_channel_id=None, reason="no_bot_token_in_env")

    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "VeasLiveVoice (https://veas.local, 0.1)",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            channel_res = await client.post(
                f"{_API_BASE}/users/@me/channels",
                json={"recipient_id": discord_user_id},
                headers=headers,
            )
            if channel_res.status_code >= 400:
                logger.warning(
                    "discord_dm: open-channel failed status=%s body=%s",
                    channel_res.status_code,
                    channel_res.text[:200],
                )
                return DmResult(
                    dispatched=False,
                    dm_channel_id=None,
                    reason=f"open_channel_{channel_res.status_code}",
                )
            payload: Any = channel_res.json()
            channel_id = str(payload.get("id") or "")
            if not channel_id:
                return DmResult(dispatched=False, dm_channel_id=None, reason="open_channel_no_id")

            send_res = await client.post(
                f"{_API_BASE}/channels/{channel_id}/messages",
                json={"content": content},
                headers=headers,
            )
            if send_res.status_code >= 400:
                logger.warning(
                    "discord_dm: send-message failed status=%s body=%s",
                    send_res.status_code,
                    send_res.text[:200],
                )
                return DmResult(
                    dispatched=False,
                    dm_channel_id=channel_id,
                    reason=f"send_{send_res.status_code}",
                )
            return DmResult(dispatched=True, dm_channel_id=channel_id)
    except Exception as exc:
        logger.exception("discord_dm: unexpected failure for discord_id=%s", discord_user_id)
        return DmResult(dispatched=False, dm_channel_id=None, reason=f"exception:{type(exc).__name__}")
