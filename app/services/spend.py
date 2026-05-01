"""LLM spend-cap helpers."""

from decimal import Decimal
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _cap_for(provider: str) -> Decimal:
    settings = get_settings()
    caps = {
        "text": settings.text_llm_daily_cap_usd,
        "vision": settings.vision_daily_cap_usd,
        "transcription": settings.transcription_daily_cap_usd,
    }
    if provider not in caps:
        raise ValueError(f"Unknown LLM spend provider: {provider}")
    return Decimal(str(caps[provider]))


async def record_llm_cost(pool: Any, provider: str, dollars: float | Decimal) -> None:
    await pool.execute(
        """
        INSERT INTO llm_spend_log (provider, day, total_usd)
        VALUES ($1, CURRENT_DATE, $2)
        ON CONFLICT (provider, day)
        DO UPDATE SET
            total_usd = llm_spend_log.total_usd + EXCLUDED.total_usd,
            updated_at = now()
        """,
        provider,
        Decimal(str(dollars)),
    )
    total = Decimal(
        str(
            await pool.fetchval(
                """
                SELECT total_usd
                FROM llm_spend_log
                WHERE provider = $1
                  AND day = CURRENT_DATE
                """,
                provider,
            )
            or 0
        )
    )
    cap = _cap_for(provider)
    if cap > 0 and total >= cap * Decimal("0.80"):
        warned_at = await pool.fetchval(
            """
            SELECT warned_80_at
            FROM llm_spend_log
            WHERE provider = $1
              AND day = CURRENT_DATE
            """,
            provider,
        )
        if warned_at is not None:
            return
        await pool.execute(
            """
            UPDATE llm_spend_log
            SET warned_80_at = COALESCE(warned_80_at, now())
            WHERE provider = $1
              AND day = CURRENT_DATE
              AND warned_80_at IS NULL
            """,
            provider,
        )
        logger.warning("LLM spend for provider=%s crossed 80%% of daily cap: total=%s cap=%s", provider, total, cap)


async def is_under_cap(pool: Any, provider: str) -> bool:
    total = await pool.fetchval(
        """
        SELECT total_usd
        FROM llm_spend_log
        WHERE provider = $1
          AND day = CURRENT_DATE
        """,
        provider,
    )
    return Decimal(str(total or 0)) < _cap_for(provider)
