"""Plan 4/5 hook contracts: OOB verdicts are ok, rewrite, or block."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.services.oob_check import check_oob_with_policy
from app.services import system_state

logger = logging.getLogger(__name__)

CheckOOB = Callable[..., Awaitable[dict[str, Any]]]


async def _default_check_oob(
    pool: Any,
    content: str,
    recipient_id: UUID,
    protected_owner_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    result = await check_oob_with_policy(
        pool,
        content=content,
        recipient_id=recipient_id,
        protected_owner_ids=protected_owner_ids,
    )
    return result.model_dump(mode="json")


check_oob: CheckOOB | None = _default_check_oob
_pool: Any | None = None


def set_pool(pool: Any | None) -> None:
    global _pool
    _pool = pool


async def paused_for_user(user_id: UUID, *, bot_id: str | None = None) -> bool:
    if _pool is None:
        return False
    logger.debug("paused_for_user check", extra={"user_id": str(user_id), "bot_id": bot_id})
    # TODO(S2b): when bot_id is not None, also call user_bot_paused(pool, user_id, bot_id)
    return await system_state.is_paused(_pool)
