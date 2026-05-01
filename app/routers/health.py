"""Health router."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import get_pool, ping


router = APIRouter()
_anthropic_cache: dict[str, Any] = {"checked_at": None, "ok": None, "error": None}


@router.get("/health", response_model=None)
async def health(pool: Any = Depends(get_pool)) -> Any:
    try:
        await ping(pool)
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "db": "error", "error": str(exc)},
        )
    return {"status": "ok", "db": "ok"}


async def _check_anthropic() -> dict[str, Any]:
    now = datetime.now(UTC)
    checked_at = _anthropic_cache.get("checked_at")
    if checked_at is not None and now - checked_at < timedelta(seconds=60):
        return {
            "status": "ok" if _anthropic_cache["ok"] else "error",
            "cached": True,
            "error": _anthropic_cache.get("error"),
        }
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": settings.anthropic_api_key.get_secret_value(),
                    "anthropic-version": "2023-06-01",
                },
            )
        response.raise_for_status()
    except Exception as exc:
        _anthropic_cache.update(checked_at=now, ok=False, error=str(exc))
        return {"status": "error", "cached": False, "error": str(exc)}
    _anthropic_cache.update(checked_at=now, ok=True, error=None)
    return {"status": "ok", "cached": False, "error": None}


@router.get("/health/deep", response_model=None)
async def deep_health(pool: Any = Depends(get_pool)) -> Any:
    checked_at = datetime.now(UTC).isoformat()
    summary: dict[str, Any] = {"status": "ok", "checked_at": checked_at}
    try:
        await ping(pool)
        summary["db"] = {"status": "ok"}
    except Exception as exc:
        summary["db"] = {"status": "error", "error": str(exc)}
    summary["anthropic"] = await _check_anthropic()
    if summary["db"]["status"] != "ok" or summary["anthropic"]["status"] != "ok":
        summary["status"] = "error"
        return JSONResponse(status_code=503, content=summary)
    return summary
