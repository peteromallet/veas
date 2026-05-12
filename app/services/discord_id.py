"""Discord bot user-id helpers — single source of truth.

Do NOT add a field to `app/config.py` Settings; use these helpers instead.
"""

from __future__ import annotations

import base64
import logging
import os
import re

logger = logging.getLogger(__name__)

_DISCORD_BOT_USER_ID_RE = re.compile(r"^\d+$")


def _decode_discord_user_id(token: str) -> str | None:
    """Decode the user-id prefix of a Discord bot token.

    Discord bot tokens are "<base64url(user_id)>.<timestamp>.<hmac>". The first
    segment is the user id encoded as urlsafe base64 (no padding). Decode it to
    the canonical decimal user id (e.g. '1245222614276898866') which is what
    inbound webhooks use and what `routing.resolve_bot` will need.
    """
    prefix = token.split(".", 1)[0]
    if not prefix:
        return None
    # base64 needs len-multiple-of-4 input; add the missing padding.
    padding = "=" * (-len(prefix) % 4)
    try:
        decoded = base64.urlsafe_b64decode(prefix + padding).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if _DISCORD_BOT_USER_ID_RE.match(decoded):
        return decoded
    return None


def discord_bot_user_id() -> str | None:
    """Return the Discord bot's numeric user id.

    Reads DISCORD_BOT_USER_ID from the environment if set and digit-only;
    otherwise falls back to decoding it from DISCORD_BOT_TOKEN via
    _decode_discord_user_id.  Returns None when neither source is available.
    """
    from_env = os.environ.get("DISCORD_BOT_USER_ID")
    if from_env and _DISCORD_BOT_USER_ID_RE.match(from_env):
        return from_env
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        return None
    return _decode_discord_user_id(token)