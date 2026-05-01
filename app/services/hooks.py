"""Plan 4/5 hook contracts: OOB verdicts are ok, rewrite, or block."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.services.oob_check import check_oob_with_policy
from app.services import system_state

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


async def paused_for_user(user_id: UUID) -> bool:
    if _pool is None:
        return False
    return await system_state.is_paused(_pool)
