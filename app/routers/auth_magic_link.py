"""HTTP surface for the Discord magic-link auth flow (R5).

POST /api/auth/discord-magic-link/request — body: `{discord_id}`.
POST /api/auth/discord-magic-link/verify  — body: `{discord_id, code}`.

Returns a short-lived JWT on successful verify. Frontend stores it in
sessionStorage and sends it in `Authorization: Bearer …`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.db import get_pool
from app.services.auth.magic_link import (
    DEFAULT_TTL_MINUTES,
    request_magic_link,
    verify_magic_link,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/discord-magic-link", tags=["auth"])


class RequestBody(BaseModel):
    discord_id: str = Field(..., min_length=10, max_length=32, pattern=r"^\d+$")


class VerifyBody(BaseModel):
    discord_id: str = Field(..., min_length=10, max_length=32, pattern=r"^\d+$")
    code: str = Field(..., min_length=4, max_length=8, pattern=r"^\d+$")


@router.post("/request")
async def request_endpoint(body: RequestBody, pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Request a code. Response is intentionally generic to avoid leaking
    whether the discord_id maps to a known user — opaque success either way.
    """
    result = await request_magic_link(pool, body.discord_id)
    return {
        "ok": True,
        "ttl_minutes": DEFAULT_TTL_MINUTES,
        "dispatched": result.dispatched,
        # `issued` is true even for "unknown_user" to prevent enumeration.
        # The frontend doesn't get to distinguish — only success/fail at
        # verify time matters.
    }


@router.post("/verify")
async def verify_endpoint(body: VerifyBody, pool: Any = Depends(get_pool)) -> dict[str, Any]:
    result = await verify_magic_link(pool, body.discord_id, body.code)
    if not result.success:
        return {"ok": False, "reason": result.reason or "verify_failed"}
    return {
        "ok": True,
        "user_id": result.user_id,
        "token": result.token,
    }
